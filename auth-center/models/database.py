#!/usr/bin/env python3
"""auth-center: Unified Database Manager - PostgreSQL edition."""
import os, logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, '..', '..', 'data')
DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA_DIR, 'x7k2m9a4.db'))
SHOP_DB_PATH = os.path.join(DATA_DIR, 'shop.db')
os.makedirs(DATA_DIR, exist_ok=True)

# 国际化：当前市场
MARKET = os.environ.get('DEPLOY_MARKET', 'cn')

# ── Shop 表名列表 ──
SHOP_TABLES = [
    'products', 'categories', 'carts', 'user_purchases', 'order_items',
    'product_specs', 'product_spec_values', 'product_skus',
    'pricing_rules', 'express_companies', 'order_shipping',
]

# PostgreSQL 连接配置
PG_CONFIG = {
    'host': os.environ.get('PG_HOST', 'localhost'),
    'port': int(os.environ.get('PG_PORT', 5432)),
    'dbname': os.environ.get('PG_DB', 'appdb'),
    'user': os.environ.get('PG_USER', 'app'),
    'password': os.environ.get('PG_PASSWORD', ''),
    'application_name': 'app',
    'connect_timeout': 10,  # 建连最多等 10 秒，避免低配机器上无限挂死
}

# ── 数据库连接 ──

# P2-F08: 连接池 — 避免高并发下耗尽 PG 连接
try:
    from psycopg2 import pool
    _connection_pool = pool.ThreadedConnectionPool(
        minconn=int(os.environ.get('PG_POOL_MIN', 2)),
        maxconn=int(os.environ.get('PG_POOL_MAX', 10)),
        **PG_CONFIG
    )
    _pool_available = True
except Exception:
    _connection_pool = None
    _pool_available = False


def _connect():
    """Create a psycopg2 connection (pooled if available, otherwise fresh)."""
    if _pool_available:
        try:
            return _connection_pool.getconn()
        except pool.PoolError:
            logger.warning("Connection pool exhausted, falling back to direct connect")
    return psycopg2.connect(**PG_CONFIG)


class _DbWrapper:
    """psycopg2 connection wrapper that exposes sqlite3-style execute/commit."""
    def __init__(self, conn):
        self._conn = conn
        self._cur = conn.cursor(cursor_factory=RealDictCursor)
        self._from_pool = _pool_available

    def execute(self, sql, params=None):
        if params is not None:
            self._cur.execute(sql, params)
        else:
            self._cur.execute(sql)
        return self

    def executemany(self, sql, params):
        return self._cur.executemany(sql, params)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def cursor(self):
        """Return a new cursor (for compatibility with direct conn usage)."""
        return self._conn.cursor()

    def close(self):
        self._cur.close()
        # P2-F08: 归还连接到池，否则关闭
        if self._from_pool and _pool_available:
            try:
                _connection_pool.putconn(self._conn)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
        else:
            try:
                self._conn.close()
            except Exception:
                pass

    def executescript(self, sql):
        """Run multi-statement SQL (psycopg2 has no native executescript)."""
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
        old_level = self._conn.isolation_level
        self._conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = self._conn.cursor()
        cur.execute(sql)
        cur.close()
        self._conn.set_isolation_level(old_level)

    def __getattr__(self, name):
        return getattr(self._cur, name)


@contextmanager
def get_db():
    """Get PostgreSQL connection, with schema search_path set."""
    conn = _connect()
    conn.autocommit = False
    try:
        conn.rollback()  # defensive: clear any aborted transaction from prev usage
    except Exception:
        pass
    db = _DbWrapper(conn)
    db.execute(
        "SET search_path TO public, shop, analytics, health, payment, order_notify"
    )
    try:
        yield db
        try:
            conn.commit()
        except Exception:
            # Swallow commit failure left over from a caught DB error (e.g. a
            # migration touched a not-yet-created table on a fresh install).
            # Roll back so the caller can continue instead of aborting the import.
            logger.exception("DB commit failed, rolled back (caller continues)")
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        # 连接归还/关闭由 _DbWrapper.close() 统一处理：
        # 池连接 → putconn 归还；非池连接 → close()。此处不得再 close，
        # 否则会把已归还池的连接二次关闭，导致下次 getconn() 拿到 closed 连接。
        db.close()


# ── Module-level migration safe wrapper ──
class _NoOpDb:
    def execute(self, *a, **kw): return _NoOpCursor()
    def commit(self): pass
    def rollback(self): pass
    def cursor(self): return _NoOpCursor()
    def executescript(self, *a, **kw): pass
    def fetchone(self, *a, **kw): return None
    def fetchall(self, *a, **kw): return []
    def __getattr__(self, name):
        return lambda *a, **kw: (_NoOpCursor() if 'execute' in name else None)

class _NoOpCursor:
    def execute(self, *a, **kw): pass
    def fetchone(self): return None
    def fetchall(self): return []
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

@contextmanager
def _safe_get_db_for_migration():
    db = None
    try:
        with get_db() as db:
            yield db
    except (psycopg2.InterfaceError, psycopg2.OperationalError, psycopg2.errors.UniqueViolation) as e:
        # UniqueViolation：多 worker 并发执行模块级 CREATE TABLE IF NOT EXISTS 时，
        # PostgreSQL 的 IF NOT EXISTS 非原子，双方同时通过存在性检查 → 一方撞
        # pg_type_typname_nsp_index 唯一约束。此时表已由并发进程建好，安全跳过。
        logger.warning(f"Module-level migration skipped (DB not ready / concurrent DDL): {e}")
        if db is None:
            # 连接阶段就失败（DB 未就绪/已关闭）→ 用 no-op 实现走完模块级语句
            yield _NoOpDb()
        # 异常来自 with 体（并发 DDL UniqueViolation 等）→ 停止生成器，
        # contextlib 判定正常结束并吞掉异常，模块正常导入；下次启动自动补迁移。
        # 注意：此处绝不能再次 yield，否则 contextlib 抛
        # "generator didn't stop after throw()" 导致 worker 启动崩溃。
        return

# ── 列信息兼容层：替代 PRAGMA table_info() ──
def get_table_columns(conn, table: str) -> list[str]:
    """Return list of column names for a table (PG-compatible)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name=%s AND table_schema=ANY(current_schemas(false)) "
        "ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def init_shop_db():
    """
    [DEPRECATED] Shop tables are now initialized by ShopPlugin (plugins/shop/).
    This function is kept for backward compatibility with existing code that
    imports it from auth-center/models. New code should use ShopPlugin.on_enable().
    """
    """Create shop tables in shop schema."""
    pass  # [DEPRECATED] Moved to plugins/shop/models/database.py


# 迁移守卫：同一进程内 init_db 只执行一次；跨进程仅一个进程执行全部 DDL。
# 多 worker 并发跑数百条 DDL 会拖垮 PG（低配机器上表现为启动"挂死"超时）。
_INIT_DB_RUNNING = False
_INIT_DB_LOCK_KEY = 72715620


def init_db():
    """Initialize all core tables using a fresh direct connection (not pool) to avoid aborted transactions."""
    global _INIT_DB_RUNNING
    if _INIT_DB_RUNNING:
        print('[init_db] skipped (already ran in this process)')
        return
    import psycopg2
    from psycopg2.extras import RealDictCursor
    fresh_conn = psycopg2.connect(**PG_CONFIG)
    fresh_conn.autocommit = False
    try:
        # 跨进程串行化：拿不到 advisory lock 说明另一进程正在跑迁移，直接跳过
        cur = fresh_conn.cursor()
        cur.execute('SELECT pg_try_advisory_lock(%s)', (_INIT_DB_LOCK_KEY,))
        if not cur.fetchone()[0]:
            print('[init_db] skipped (another process holds migration lock)')
            cur.close()
            fresh_conn.close()
            return
        cur.close()
        _INIT_DB_RUNNING = True
        # ── Mega DDL block: 核心表（隔离在单独事务中，失败不级联）──
        cur = fresh_conn.cursor()
        cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                username        TEXT UNIQUE,
                phone           TEXT UNIQUE,
                phone_verified  BIGINT DEFAULT 0,
                email           TEXT UNIQUE,
                password_hash   TEXT,
                wechat_openid   TEXT UNIQUE,
                wechat_unionid  TEXT,
                wechat_nickname TEXT,
                douyin_open_id  TEXT UNIQUE,
                douyin_nickname TEXT,
                douyin_avatar   TEXT,
                avatar_url      TEXT,
                created_at      TIMESTAMP DEFAULT NOW(),
                last_login      TIMESTAMP,
                active          BIGINT DEFAULT 1,
                is_admin        BIGINT DEFAULT 0,
                agent_id        TEXT UNIQUE,
                agent_nickname  TEXT DEFAULT '',
                agent_avatar_url TEXT DEFAULT '',
                display_name      TEXT DEFAULT '',
                email_verified    BIGINT DEFAULT 0,
                password_changed_at TIMESTAMP,
                totp_secret       TEXT DEFAULT '',
                totp_enabled      BIGINT DEFAULT 0,
                security_level    BIGINT DEFAULT 0,
                completion_percentage  BIGINT DEFAULT 0,
                completion_last_updated TIMESTAMP,
                alipay_user_id          TEXT UNIQUE,
                telegram_open_id        TEXT UNIQUE,
                enterprise_name         TEXT DEFAULT '',
                enterprise_tax_id       TEXT DEFAULT '',
                enterprise_address      TEXT DEFAULT '',
                enterprise_phone        TEXT DEFAULT '',
                enterprise_bank         TEXT DEFAULT '',
                enterprise_bank_acct    TEXT DEFAULT '',
                enterprise_verified     BIGINT DEFAULT 0,
                enterprise_verified_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);
            CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);
            CREATE TABLE IF NOT EXISTS user_profiles (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL UNIQUE REFERENCES users(id),
                gender          TEXT DEFAULT '' CHECK(gender IN ('', 'male', 'female', 'other', 'secret')),
                birth_date      TEXT DEFAULT NULL,
                age_group       TEXT DEFAULT '',
                occupation      TEXT DEFAULT '',
                industry        TEXT DEFAULT '',
                interests       TEXT DEFAULT '[]',
                bio             TEXT DEFAULT '',
                industry_id     BIGINT DEFAULT NULL,
                career_id       BIGINT DEFAULT NULL,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS industries (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                sort_order  BIGINT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS career_options (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                category    TEXT NOT NULL CHECK(category IN ('job', 'freelance')),
                name        TEXT NOT NULL UNIQUE,
                industry_id BIGINT DEFAULT NULL REFERENCES industries(id) ON DELETE SET NULL,
                parent_id   BIGINT DEFAULT NULL REFERENCES career_options(id) ON DELETE CASCADE,
                sort_order  BIGINT DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_career_options_category ON career_options(category);
            CREATE INDEX IF NOT EXISTS idx_career_options_parent   ON career_options(parent_id);
            CREATE TABLE IF NOT EXISTS user_addresses (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL REFERENCES users(id),
                recipient_name  TEXT NOT NULL DEFAULT '',
                phone           TEXT NOT NULL DEFAULT '',
                province_code   TEXT NOT NULL DEFAULT '',
                city_code       TEXT NOT NULL DEFAULT '',
                district_code   TEXT NOT NULL DEFAULT '',
                street_code     TEXT NOT NULL DEFAULT '',
                street_address  TEXT NOT NULL DEFAULT '',
                postal_code     TEXT DEFAULT '',
                is_default      BIGINT DEFAULT 0,
                status          BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_user_addresses_user ON user_addresses(user_id);
            CREATE TABLE IF NOT EXISTS app_authorizations (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                app_name        TEXT NOT NULL,
                tier            TEXT DEFAULT 'free',
                tier_expire_at  TIMESTAMP,
                calls_today     BIGINT DEFAULT 0,
                calls_total     BIGINT DEFAULT 0,
                last_reset      TIMESTAMP,
                active          BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, app_name)
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                app_name        TEXT NOT NULL,
                key_hash        TEXT UNIQUE NOT NULL,
                key_prefix      TEXT NOT NULL,
                name            TEXT DEFAULT '',
                calls_today     BIGINT DEFAULT 0,
                calls_total     BIGINT DEFAULT 0,
                last_reset      TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW(),
                expire_at       TIMESTAMP,
                last_used       TIMESTAMP,
                active          BIGINT DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS system_config (
                key             TEXT PRIMARY KEY,
                value           TEXT NOT NULL DEFAULT '',
                description     TEXT DEFAULT '',
                updated_at      TIMESTAMP DEFAULT NOW(),
                updated_by      BIGINT DEFAULT 0
            );
            INSERT INTO system_config (key, value, description) VALUES
                ('default_language', 'zh-CN', 'Default system language'),
                ('default_timezone', 'Asia/Shanghai', 'Default timezone'),
                ('site_name', 'VeroRun', 'Site display name'),
                ('admin_email', '', 'Admin contact email'),
                ('maintenance_mode', '0', 'System maintenance mode')
            ON CONFLICT (key) DO NOTHING;
            CREATE TABLE IF NOT EXISTS user_notifications (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                type            TEXT NOT NULL DEFAULT 'system',
                title           TEXT NOT NULL,
                content         TEXT DEFAULT '',
                link_url        TEXT DEFAULT '',
                is_read         BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS notification_preferences (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL UNIQUE REFERENCES users(id),
                prefs           TEXT DEFAULT '{}',   -- JSON: {"system_site":true,"system_mail":true,"order_site":true,"order_mail":true,"activity_site":true,"activity_mail":false}
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS user_agents (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL REFERENCES users(id),
                agent_name      TEXT NOT NULL DEFAULT '',
                agent_type      TEXT NOT NULL DEFAULT 'personal',  -- personal / trading
                avatar_url      TEXT DEFAULT '',
                status          TEXT DEFAULT 'active',  -- active / inactive / suspended
                default_scopes  TEXT DEFAULT '[]',      -- JSON: ["stock:read","market:alert"]
                metadata        TEXT DEFAULT '{}',      -- JSON: non-privacy business data
                last_active_ip  TEXT DEFAULT '',
                last_active_at  TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_user_agents_user ON user_agents(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_agents_name ON user_agents(user_id, agent_name);
            
            CREATE TABLE IF NOT EXISTS agent_api_keys (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                agent_id        BIGINT NOT NULL REFERENCES user_agents(id),
                user_id         BIGINT NOT NULL REFERENCES users(id),
                key_hash        TEXT UNIQUE NOT NULL,
                key_prefix      TEXT NOT NULL,
                name            TEXT DEFAULT '',
                scopes          TEXT DEFAULT '[]',      -- JSON override, empty=inherit from agent
                status          TEXT DEFAULT 'active',   -- active / revoked / expired
                expire_at       TIMESTAMP,
                last_used_at    TIMESTAMP,
                rotated_at      TIMESTAMP,
                rotated_from_key_id BIGINT DEFAULT 0,
                calls_today     BIGINT DEFAULT 0,
                calls_total     BIGINT DEFAULT 0,
                last_reset      TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_agent_keys_agent ON agent_api_keys(agent_id);
            CREATE INDEX IF NOT EXISTS idx_agent_keys_user ON agent_api_keys(user_id);
            CREATE INDEX IF NOT EXISTS idx_agent_keys_hash ON agent_api_keys(key_hash);

            CREATE TABLE IF NOT EXISTS agent_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                agent_id        BIGINT REFERENCES user_agents(id),
                user_id         BIGINT REFERENCES users(id),
                action          TEXT NOT NULL,  -- create / revoke_key / rotate_key / suspend / activate
                detail          TEXT DEFAULT '',
                ip_address      TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_agent_logs_agent ON agent_logs(agent_id);
            CREATE INDEX IF NOT EXISTS idx_agent_logs_user ON agent_logs(user_id);

            CREATE TABLE IF NOT EXISTS user_sessions (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL REFERENCES users(id),
                token_hash      TEXT NOT NULL,
                device_name     TEXT DEFAULT '',
                device_type     TEXT DEFAULT '',  -- mobile / desktop / api
                ip_address      TEXT DEFAULT '',
                user_agent      TEXT DEFAULT '',
                location        TEXT DEFAULT '',
                is_current      BIGINT DEFAULT 0,
                last_active     TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW(),
                expired_at      TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);
            
            CREATE TABLE IF NOT EXISTS agent_experiences (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                agent_id        TEXT NOT NULL,
                title           TEXT NOT NULL,
                content         TEXT NOT NULL,
                category        TEXT DEFAULT 'analysis',
                tags            TEXT DEFAULT '',
                status          TEXT DEFAULT 'draft',
                is_published    BIGINT DEFAULT 0,
                like_count      BIGINT DEFAULT 0,
                view_count      BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS favorites (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                target_type     TEXT NOT NULL,
                target_id       BIGINT NOT NULL,
                created_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, target_type, target_id)
            );
            CREATE TABLE IF NOT EXISTS user_activity (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                type            TEXT NOT NULL DEFAULT 'system',
                title           TEXT NOT NULL,
                content         TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS admin_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                admin_id        BIGINT REFERENCES users(id),
                action          TEXT NOT NULL,
                target_type     TEXT DEFAULT '',
                target_id       TEXT DEFAULT '',
                detail          TEXT DEFAULT '',
                ip_address      TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS sms_templates (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                category        TEXT NOT NULL,
                name            TEXT NOT NULL,
                template_code   TEXT NOT NULL,
                note            TEXT DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE(category, name)
            );
            INSERT INTO sms_templates (category, name, template_code, note, sort_order) VALUES
                ('captcha', '新用户注册',   'SMS_506350148', '新用户注册验证码', 1),
                ('captcha', '用户登录',     'SMS_506430157', '用户登录验证码', 2),
                ('captcha', '忘记/重置密码', 'SMS_506140192', '密码重置验证码', 3),
                ('captcha', '变更手机号',   'SMS_506175167', '手机号变更验证码', 4),
                ('notice',  '订阅通知',     'SMS_506235155', '会员订阅成功通知', 5),
                ('promo',   '新用户礼包',   'SMS_506455152', '新用户注册赠送优惠券通知', 6)
            ON CONFLICT (category, name) DO NOTHING;
                        CREATE TABLE IF NOT EXISTS agents (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                type            TEXT NOT NULL DEFAULT 'child',
                alias           TEXT NOT NULL,
                mission         TEXT NOT NULL DEFAULT '',
                system_prompt   TEXT NOT NULL DEFAULT '',
                -- ↓ 以下字段废弃，由 provider_models 统一管理
                provider        TEXT NOT NULL DEFAULT '',
                model_name      TEXT NOT NULL DEFAULT '',
                base_url        TEXT NOT NULL DEFAULT '',
                api_key_enc     TEXT NOT NULL DEFAULT '',
                capabilities    TEXT NOT NULL DEFAULT 'text',
                -- ↑ 废弃字段
                provider_model_id BIGINT DEFAULT NULL,
                -- ↓ 旧迁移兼容，废弃
                model_provider_id BIGINT DEFAULT NULL,
                -- ↑ 废弃
                is_active       BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            -- 模型提供商（顶层级联）
            CREATE TABLE IF NOT EXISTS providers (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                slug            TEXT NOT NULL UNIQUE DEFAULT '',
                name            TEXT NOT NULL DEFAULT '',
                description     TEXT NOT NULL DEFAULT '',
                is_active       BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            -- 提供商下的模型（端点 + Key + model_name）
            CREATE TABLE IF NOT EXISTS provider_models (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                provider_id     BIGINT NOT NULL REFERENCES providers(id),
                name            TEXT NOT NULL DEFAULT '',
                model_name      TEXT NOT NULL DEFAULT '',
                endpoint_url    TEXT NOT NULL DEFAULT '',
                api_key_ref     TEXT NOT NULL DEFAULT '',
                capabilities    TEXT NOT NULL DEFAULT 'text',
                sort_order      BIGINT DEFAULT 0,
                is_active       BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_pm_provider ON provider_models(provider_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_provider_model_unique ON provider_models(provider_id, model_name);
            CREATE TABLE IF NOT EXISTS billing_orders (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                order_no        TEXT UNIQUE NOT NULL,
                amount          DOUBLE PRECISION NOT NULL DEFAULT 0,
                currency        TEXT DEFAULT 'CNY',
                item_type       TEXT NOT NULL,
                item_desc       TEXT DEFAULT '',
                status          TEXT DEFAULT 'pending',
                payment_method  TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW(),
                paid_at         TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_billing_orders_status ON billing_orders(status);
            CREATE INDEX IF NOT EXISTS idx_billing_orders_paid ON billing_orders(status, paid_at);


            CREATE TABLE IF NOT EXISTS sms_codes (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                phone           TEXT NOT NULL,
                code            TEXT NOT NULL,
                purpose         TEXT DEFAULT 'login',
                expires_at      TEXT NOT NULL,
                used            BIGINT DEFAULT 0,
                attempts        BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS sms_rate_limits (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                phone           TEXT NOT NULL,
                hour_bucket     TEXT NOT NULL,
                count           BIGINT DEFAULT 0,
                UNIQUE(phone, hour_bucket)
            );
            CREATE TABLE IF NOT EXISTS email_codes (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                email           TEXT NOT NULL,
                code            TEXT NOT NULL,
                purpose         TEXT DEFAULT 'login',
                expires_at      TEXT NOT NULL,
                used            BIGINT DEFAULT 0,
                attempts        BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_email_code ON email_codes(email, code, purpose);
            CREATE TABLE IF NOT EXISTS login_attempts (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                phone           TEXT DEFAULT '',
                ip              TEXT NOT NULL DEFAULT '',
                success         BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip);
            CREATE INDEX IF NOT EXISTS idx_login_attempts_phone ON login_attempts(phone);
            CREATE TABLE IF NOT EXISTS orders (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                app_name        TEXT NOT NULL,
                order_id        TEXT UNIQUE NOT NULL,
                tier_bought     TEXT,
                amount          DOUBLE PRECISION,
                pay_method      TEXT,
                status          TEXT DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT NOW(),
                paid_at         TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS chat_history (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                app_name        TEXT DEFAULT 'main',
                session_id      TEXT,
                role            TEXT,
                content         TEXT,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_app_auth_user ON app_authorizations(user_id);
            CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_codes(phone);
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

            CREATE TABLE IF NOT EXISTS site_configs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                domain          TEXT NOT NULL UNIQUE,
                name            TEXT NOT NULL,
                industry        TEXT NOT NULL DEFAULT '',
                theme_color     TEXT DEFAULT '#6366f1',
                accent_color    TEXT DEFAULT '#8b5cf6',
                logo_url        TEXT DEFAULT '',
                favicon_url     TEXT DEFAULT '',
                tier            TEXT DEFAULT 'free',
                features        TEXT DEFAULT '[]',
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_site_configs_domain ON site_configs(domain);

            CREATE TABLE IF NOT EXISTS site_blocks (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site_id         BIGINT NOT NULL REFERENCES site_configs(id),
                page            TEXT NOT NULL,
                section         TEXT NOT NULL,
                block_type      TEXT NOT NULL DEFAULT 'text',
                position        BIGINT NOT NULL DEFAULT 0,
                title           TEXT DEFAULT '',
                subtitle        TEXT DEFAULT '',
                content         TEXT DEFAULT '',
                image_url       TEXT DEFAULT '',
                link_url        TEXT DEFAULT '',
                link_text       TEXT DEFAULT '',
                icon            TEXT DEFAULT '',
                extra_json      TEXT DEFAULT '{}',
                is_published    BIGINT NOT NULL DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_site_blocks_site ON site_blocks(site_id, page, position);

            CREATE TABLE IF NOT EXISTS site_plans (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site_id         BIGINT NOT NULL REFERENCES site_configs(id),
                name            TEXT NOT NULL,
                tier            TEXT NOT NULL DEFAULT 'free',
                price           DOUBLE PRECISION NOT NULL DEFAULT 0,
                period          TEXT DEFAULT 'month',
                features        TEXT DEFAULT '[]',
                sort_order      BIGINT DEFAULT 0,
                is_published    BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_site_plans_site ON site_plans(site_id);
            CREATE TABLE IF NOT EXISTS contact_messages (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                email           TEXT NOT NULL,
                subject         TEXT NOT NULL,
                message         TEXT NOT NULL,
                status          TEXT DEFAULT 'unread',
                admin_reply     TEXT,
                replied_at      TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS user_feedback (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                type            TEXT NOT NULL DEFAULT 'suggestion',
                category        TEXT NOT NULL DEFAULT 'other',
                title           TEXT NOT NULL,
                content         TEXT NOT NULL,
                contact         TEXT DEFAULT '',
                status          TEXT DEFAULT 'pending',
                admin_note      TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS user_tickets (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT REFERENCES users(id),
                type            TEXT DEFAULT 'aftersale',
                category        TEXT DEFAULT '',
                title           TEXT NOT NULL,
                content         TEXT NOT NULL,
                contact         TEXT DEFAULT '',
                status          TEXT DEFAULT 'open',
                priority        TEXT DEFAULT 'normal',
                admin_reply     TEXT DEFAULT '',
                replied_at      TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_user_tickets_user ON user_tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_tickets_status ON user_tickets(status);
            CREATE TABLE IF NOT EXISTS email_sent (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                from_addr       TEXT NOT NULL,
                to_addr         TEXT NOT NULL,
                subject         TEXT NOT NULL,
                body_text       TEXT,
                body_html       TEXT,
                in_reply_to     BIGINT,
                sent_at         TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_contact_status ON contact_messages(status);
            CREATE INDEX IF NOT EXISTS idx_email_sent_from ON email_sent(from_addr);
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id);
            CREATE TABLE IF NOT EXISTS social_push_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                platform        TEXT NOT NULL DEFAULT 'wechat',
                content_type    TEXT DEFAULT 'article',
                title           TEXT DEFAULT '',
                summary         TEXT DEFAULT '',
                article_json    TEXT DEFAULT '',
                media_id        TEXT DEFAULT '',
                publish_id      TEXT DEFAULT '',
                status          TEXT DEFAULT 'draft',
                push_time       TIMESTAMP,
                admin_id        BIGINT REFERENCES users(id),
                error_msg       TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS brand_settings (
                id              BIGINT PRIMARY KEY CHECK (id = 1),
                company_name    TEXT NOT NULL DEFAULT '',
                site_name_cn    TEXT NOT NULL DEFAULT '',
                site_name_en    TEXT NOT NULL DEFAULT '',
                slogan          TEXT NOT NULL DEFAULT '',
                tagline         TEXT NOT NULL DEFAULT '',
                description     TEXT NOT NULL DEFAULT '',
                copyright       TEXT NOT NULL DEFAULT '',
                seo_title       TEXT NOT NULL DEFAULT '',
                seo_desc        TEXT NOT NULL DEFAULT '',
                logo_url        TEXT NOT NULL DEFAULT '',
                favicon_url     TEXT NOT NULL DEFAULT '',
                icp_number      TEXT NOT NULL DEFAULT '',
                security_number TEXT NOT NULL DEFAULT '',
                contact_email   TEXT NOT NULL DEFAULT '',
                software_name   TEXT NOT NULL DEFAULT '',
                software_slogan TEXT NOT NULL DEFAULT '',
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            INSERT INTO brand_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
        """)
        fresh_conn.commit()
        # -- social_links: 后台社媒图标管理 --
        with get_db() as c2:
            c2.execute("""
                CREATE TABLE IF NOT EXISTS social_links (
                    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    name            TEXT NOT NULL,
                    url             TEXT NOT NULL DEFAULT '#',
                    icon_url        TEXT NOT NULL DEFAULT '',
                    platform        TEXT NOT NULL DEFAULT '',
                    sort_order      BIGINT DEFAULT 0,
                    is_active       BIGINT DEFAULT 1,
                    created_at      TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """)
            c2.commit()
        # service_plans 表 — 套餐管理
        with get_db() as c3:
            c3.execute("""
                CREATE TABLE IF NOT EXISTS service_plans (
                    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    plan_key        TEXT UNIQUE NOT NULL,
                    name            TEXT NOT NULL,
                    description     TEXT DEFAULT '',
                    price_month     DOUBLE PRECISION DEFAULT 0,
                    price_year      DOUBLE PRECISION DEFAULT 0,
                    daily_limit     BIGINT DEFAULT 0,
                    features        TEXT DEFAULT '[]',
                    sort_order      BIGINT DEFAULT 0,
                    is_active       BIGINT DEFAULT 1,
                    created_at      TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """)
            c3.execute("INSERT INTO service_plans (plan_key, name, description, price_month, price_year, daily_limit, features, sort_order) VALUES "
                       "('free', 'Free', '每日20次调用', 0, 0, 20, '[\"basic\"]', 1) ON CONFLICT (plan_key) DO NOTHING")
            c3.execute("INSERT INTO service_plans (plan_key, name, description, price_month, price_year, daily_limit, features, sort_order) VALUES "
                       "('standard', 'Standard', '每日100次调用', 88, 888, 100, '[\"basic\",\"sentiment\",\"market\"]', 2) ON CONFLICT (plan_key) DO NOTHING")
            c3.execute("INSERT INTO service_plans (plan_key, name, description, price_month, price_year, daily_limit, features, sort_order) VALUES "
                       "('pro', 'Pro', '每日1000次调用', 188, 1888, 1000, '[\"all\"]', 3) ON CONFLICT (plan_key) DO NOTHING")
            c3.commit()
        # ── 管理员配置表 (2026-05-10) ──
        cur = fresh_conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_profiles (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT UNIQUE REFERENCES users(id),
                role            TEXT DEFAULT 'admin',           -- super_admin / admin / operator
                permissions     TEXT DEFAULT '[]',              -- JSON array
                real_name       TEXT DEFAULT '',
                internal_phone  TEXT DEFAULT '',
                internal_email  TEXT DEFAULT '',
                notes           TEXT DEFAULT '',
                created_by      BIGINT DEFAULT 0,
                last_login_ip   TEXT DEFAULT '',
                last_login_at   TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        fresh_conn.commit()
        print('[Migration] admin_profiles table created', flush=True)
        # ── 主题管理 (2026-05-16) ──
        with get_db() as c_th:
            c_th.execute("""
                CREATE TABLE IF NOT EXISTS themes (
                    id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name            TEXT NOT NULL,
                    slug            TEXT UNIQUE NOT NULL,
                    version         TEXT DEFAULT '1.0.0',
                    author          TEXT DEFAULT '',
                    author_url      TEXT DEFAULT '',
                    description     TEXT DEFAULT '',
                    industry        TEXT DEFAULT '',
                    tags            TEXT DEFAULT '[]',
                    config_json     TEXT DEFAULT '{}',
                    dir_name        TEXT NOT NULL,
                    installed_at    TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """)
            c_th.execute("""
                CREATE TABLE IF NOT EXISTS site_theme_config (
                    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    site_key        TEXT UNIQUE NOT NULL,
                    theme_id        BIGINT,
                    overrides_json  TEXT DEFAULT '{}',
                    updated_at      TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY (theme_id) REFERENCES themes(id) ON DELETE SET NULL
                )
            """)
            # Seed: default theme
            c_th.execute(
                "INSERT INTO themes (id, name, slug, version, author, description, industry, tags, config_json, dir_name) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (0, 'Default Theme', 'default', '1.0.0', '', 
                 'Built-in default theme — FinTech/AI dark sci-fi style',
                 'finance', '["dark","fintech","ai"]',
                 '{"name":"Default Theme","slug":"default","version":"1.0.0","builtin":true}',
                 'default')
            )
            # 种子：4 个站点默认使用默认主题（theme_id=NULL）
            c_th.execute("INSERT INTO site_theme_config (site_key) VALUES ('main') ON CONFLICT (site_key) DO NOTHING")
            c_th.execute("INSERT INTO site_theme_config (site_key) VALUES ('platform') ON CONFLICT (site_key) DO NOTHING")
            c_th.execute("INSERT INTO site_theme_config (site_key) VALUES ('admin') ON CONFLICT (site_key) DO NOTHING")
            # community site key removed (智体广场已下线)
            c_th.commit()
        # [P3] 旧订阅 DDL 已移除：subscription_plans / subscriptions / subscription_orders /
        # payment_events / subscription_audit_log 已随订阅解耦下线（插件 subscription schema 接管）
        fresh_conn.commit()
    except Exception as e:
        print(f'[init_db] ⚠️ Mega DDL block failed (non-critical): {e}')
    finally:
        fresh_conn.close()
    # ── 品牌设置字段迁移：logo_url → logo_full_url + 新增 logo_icon_url ──
    with get_db() as bm:
        try:
            bm.execute("ALTER TABLE brand_settings ADD COLUMN logo_full_url TEXT NOT NULL DEFAULT ''")
        except Exception:
            bm.rollback()
        try:
            bm.execute("ALTER TABLE brand_settings ADD COLUMN logo_icon_url TEXT NOT NULL DEFAULT ''")
        except Exception:
            bm.rollback()
        try:
            bm.execute("UPDATE brand_settings SET logo_full_url = logo_url WHERE logo_full_url = '' AND logo_url != ''")
            bm.commit()
        except Exception as e:
            bm.rollback()
            print(f'[Migration] brand_settings logo migration skipped: {e}')
    # ── 品牌设置字段迁移：新增 company_name / tagline / icp / security / contact_email ──
    with get_db() as bm:
        for col, default_val in [
            ('company_name',   "''"),
            ('tagline',         "''"),
            ('icp_number',      "''"),
            ('security_number', "''"),
            ('contact_email',   "''"),
        ]:
            try:
                bm.execute(f"ALTER TABLE brand_settings ADD COLUMN {col} TEXT NOT NULL DEFAULT {default_val}")
            except Exception:
                pass
        bm.commit()
    # ── Migration: brand_settings site_domain ──
    with get_db() as m:
        cols = get_table_columns(m, 'brand_settings')
        if 'site_domain' not in cols:
            m.execute("ALTER TABLE brand_settings ADD COLUMN site_domain TEXT NOT NULL DEFAULT ''")
            m.commit()
            print('[Migration] brand_settings.site_domain added')
    # ── Migration: migrate users.agent_id → user_agents (2026-05-10) ──
    with get_db() as m:
        # Check if legacy agent_id column exists in users table
        user_cols = get_table_columns(m, 'users')
        has_legacy_agent = 'agent_id' in user_cols
        
        if has_legacy_agent:
            count = m.execute('SELECT COUNT(*) as c FROM user_agents').fetchone()
            if count['c'] == 0:
                rows = m.execute(
                    "SELECT id, agent_id, agent_nickname, agent_avatar_url, display_name "
                    "FROM users WHERE agent_id IS NOT NULL AND agent_id != ''"
                ).fetchall()
                migrated = 0
                for r in rows:
                    agent_name = r['agent_nickname'] or r['display_name'] or f"agent_{r['id']}"
                    m.execute(
                        "INSERT INTO user_agents "
                        "(user_id, agent_name, agent_type, avatar_url, status, created_at) "
                        "VALUES (%s, %s, 'personal', %s, 'active', NOW())",
                        (r['id'], agent_name, r['agent_avatar_url'] or '')
                    )
                    migrated += 1
                if migrated:
                    m.commit()
                    print(f'[Migration] {migrated} user agents created from legacy agent_id')
                else:
                    print('[Migration] No legacy user agent data to migrate')
        else:
            print('[Migration] No legacy agent_id column — skipping migration')
        
        # Add agent_id FK column to api_keys if not present
        cols = get_table_columns(m, 'api_keys')
        if 'associated_agent_id' not in cols:
            try:
                m.execute('ALTER TABLE api_keys ADD COLUMN associated_agent_id BIGINT DEFAULT 0')
                m.commit()
                print('[Migration] api_keys.associated_agent_id added')
            except Exception:
                pass
    
    # Migration: add agent_avatar_url if missing
    with get_db() as m:
        # Also ensure admin_profiles users have is_admin=1
        try:
            m.execute(
                "UPDATE users SET is_admin=1 WHERE id IN ("
                "  SELECT user_id FROM admin_profiles"
                ") AND is_admin=0"
            )
            m.commit()
        except Exception:
            m.rollback()
    with get_db() as m:
        cols = get_table_columns(m, 'users')
        if 'agent_avatar_url' not in cols:
            m.execute('ALTER TABLE users ADD COLUMN agent_avatar_url TEXT DEFAULT \'\'')
            m.commit()
            print('[Migration] agents.agent_avatar_url added')

    # ── IAM v2 migration: add new columns (2026-05-11) ──
    with get_db() as m:
        cols = get_table_columns(m, 'users')
        if 'display_name' not in cols:
            for col_def in [
                ("display_name", "TEXT DEFAULT ''"),
                ("email_verified", "BIGINT DEFAULT 0"),
                ("password_changed_at", "TEXT"),
                ("totp_secret", "TEXT DEFAULT ''"),
                ("totp_enabled", "BIGINT DEFAULT 0"),
                ("security_level", "BIGINT DEFAULT 0"),
            ]:
                try:
                    m.execute(f"ALTER TABLE users ADD COLUMN {col_def[0]} {col_def[1]}")
                except Exception:
                    pass
            m.commit()
            print('[Migration] IAM v2 columns added to users table')
        # Backfill existing users
        try:
            m.execute("UPDATE users SET username = phone WHERE username IS NULL AND phone IS NOT NULL")
            m.execute("UPDATE users SET display_name = COALESCE(display_name, phone, 'User') WHERE display_name = '' OR display_name IS NULL")
            m.commit()
            print('[Migration] IAM v2 backfill complete')
        except Exception:
            m.rollback()
    # ── Real-name verification migration v2 (2026-05-19) ──
    # 合规要求：不存储身份证号（明文或加密），只存认证状态标记
    with get_db() as m:
        cols = get_table_columns(m, 'users')
        # 保留旧字段以兼容，但不再写入 id_number_encrypted
        for col_name, col_def in [
            ('verified_by', "TEXT DEFAULT ''"),
            ('verified_at', "TEXT"),
            ('id_number_encrypted', "TEXT DEFAULT ''"),
            ('is_real_name_verified', "BIGINT DEFAULT 0"),
            ('real_name_verified_at', "TEXT"),
        ]:
            if col_name not in cols:
                try:
                    m.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass
        # 清空历史遗留的加密身份证号（合规要求：不存储）
        m.execute("UPDATE users SET id_number_encrypted = '' WHERE id_number_encrypted != ''")
        m.commit()
        print('[Migration] Real-name verification v2: is_real_name_verified + real_name_verified_at added, id_number_encrypted cleared')

    # ── Verification provider config seeds (admin fills in credentials later) ──
    with get_db() as m:
        provider_seeds = [
            ('verification.provider', 'alipay', '实名认证服务商: alipay / wechat / stub'),
            ('verification.alipay.app_id', '', '支付宝开放平台 App ID'),
            ('verification.alipay.private_key', '', '支付宝应用私钥 (PKCS8)'),
            ('verification.alipay.alipay_public_key', '', '支付宝公钥'),
            ('verification.alipay.auth_url', 'https://openapi.alipay.com/gateway.do', '支付宝网关地址'),
            ('verification.alipay.return_url', '', '认证完成后回跳URL'),
            ('verification.wechat.app_id', '', '微信开放平台 App ID'),
            ('verification.wechat.app_secret', '', '微信开放平台 App Secret'),
            ('verification.wechat.auth_url', 'https://api.weixin.qq.com/sns/oauth2/access_token', '微信OAuth地址'),
            ('verification.enabled', 'false', '是否启用第三方实名认证 (true/false)'),
            ('verification.stub_mode', 'false', '开发模式：true=跳过真实第三方调用，直接模拟通过（默认关闭，安全优先）'),
        ]
        for key, value, desc in provider_seeds:
            m.execute(
                "INSERT INTO system_config (key, value, description) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING",
                (key, value, desc)
            )
        m.commit()
        print('[Migration] Verification provider config seeds added')

    # ── Verification requests log table ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS verification_requests (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id         BIGINT NOT NULL REFERENCES users(id),
                request_id      TEXT UNIQUE NOT NULL,
                provider        TEXT NOT NULL DEFAULT '',
                return_url      TEXT DEFAULT '',
                status          TEXT DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT NOW(),
                completed_at    TEXT
            )
        """)
        m.execute("CREATE INDEX IF NOT EXISTS idx_vr_request_id ON verification_requests(request_id)")
        m.execute("CREATE INDEX IF NOT EXISTS idx_vr_user_id ON verification_requests(user_id)")
        m.commit()

    # ── Migration: seed providers + provider_models (replaces model_providers) ──
    with get_db() as m:
        # Ensure new tables exist (CREATE TABLE IF NOT EXISTS handles fresh installs)
        # Ensure UNIQUE index to prevent duplicate model seeds
        m.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_provider_model_unique ON provider_models(provider_id, model_name)')
        # Seed providers
        provider_seeds = [
            ('dashscope',  '阿里云 DashScope', 'Qwen LLM, image gen, CosyVoice'),
            ('deepseek',   'DeepSeek', 'DeepSeek large language models'),
            ('openai',     'OpenAI', 'GPT-4o, DALL-E, TTS'),
            ('openrouter', 'OpenRouter', 'Multi-model aggregation router'),
            ('ollama',     'Ollama', 'Local open-source model deployment'),
            ('siliconflow','SiliconFlow', 'SiliconFlow model platform (DeepSeek-OCR etc.)'),
            ('gemini',     'Google Gemini', 'Gemini 2.5 Flash/Pro'),
            ('grok',       'xAI Grok', 'Grok-3 Beta'),
            ('kimi',       'KIMI / 月之暗面', 'Moonshot AI large language models'),
            ('zhipu',      'Zhipu / 智谱 AI', 'ChatGLM large language models'),
            ('edge_tts',   'Microsoft Edge TTS', 'Free Edge browser TTS — no key required, same neural voices'),
        ]
        for slug, name, desc in provider_seeds:
            m.execute(
                "INSERT INTO providers (slug, name, description) VALUES (%s,%s,%s) ON CONFLICT (slug) DO NOTHING",
                (slug, name, desc)
            )
        # Resolve provider IDs
        pids = {slug: m.execute("SELECT id FROM providers WHERE slug = %s", (slug,)).fetchone()['id']
                for slug, _, _ in provider_seeds}
        # Seed provider_models
        model_seeds = [
            # Alibaba Cloud DashScope
            (pids['dashscope'],  'Qwen Turbo',     'qwen-turbo',             'https://dashscope.aliyuncs.com/compatible-mode/v1',          'dashscope_text_key',    'text',     10),
            (pids['dashscope'],  'Qwen Max',        'qwen-max',               'https://dashscope.aliyuncs.com/compatible-mode/v1',          'dashscope_text_key',    'text',     11),
            (pids['dashscope'],  'Qwen Plus',       'qwen-plus',              'https://dashscope.aliyuncs.com/compatible-mode/v1',          'dashscope_text_key',    'text',     12),
            (pids['dashscope'],  'Qwen 2.5 72B',    'qwen2.5-72b-instruct',   'https://dashscope.aliyuncs.com/compatible-mode/v1',          'dashscope_text_key',    'text',     13),
            (pids['dashscope'],  'DeepSeek R1',          'deepseek-r1',            'https://dashscope.aliyuncs.com/compatible-mode/v1',          'dashscope_text_key',    'text',     14),
            (pids['dashscope'],  'DeepSeek V3',          'deepseek-v3',            'https://dashscope.aliyuncs.com/compatible-mode/v1',          'dashscope_text_key',    'text',     15),
            (pids['dashscope'],  'Image Gen Wan2.7',      'wan2.7-image',           'https://dashscope.aliyuncs.com/api/v1/services/aigc/image-generation/generation', 'dashscope_api_key', 'image', 20),
            (pids['dashscope'],  'CosyVoice Clone',   'cosyvoice-v1',           'https://dashscope.aliyuncs.com/api/v1/services/audio/tts',  'dashscope_api_key',     'voice',    21),
            # DeepSeek
            (pids['deepseek'],   'DeepSeek V4 Flash',   'deepseek-v4-flash',      'https://api.deepseek.com',                                  'deepseek_api_key',      'text',     30),
            (pids['deepseek'],   'DeepSeek V4 Pro',     'deepseek-v4-pro',        'https://api.deepseek.com',                                  'deepseek_api_key',      'text',     31),
            # OpenAI
            (pids['openai'],     'GPT-4o',               'gpt-4o',                 'https://api.openai.com/v1',                                 'openai_api_key',        'text',     40),
            (pids['openai'],     'GPT-4o Mini',          'gpt-4o-mini',            'https://api.openai.com/v1',                                 'openai_api_key',        'text',     41),
            (pids['openai'],     'GPT-4 Turbo',          'gpt-4-turbo',            'https://api.openai.com/v1',                                 'openai_api_key',        'text',     42),
            (pids['openai'],     'DALL-E 3',             'dall-e-3',               'https://api.openai.com/v1',                                 'openai_api_key',        'image',    43),
            (pids['openai'],     'TTS-1',                'tts-1',                  'https://api.openai.com/v1',                                 'openai_api_key',        'voice',    44),
            # OpenRouter
            (pids['openrouter'], 'OpenAI GPT-4o',        'openai/gpt-4o',          'https://openrouter.ai/api/v1',                              'openrouter_api_key',    'text',     50),
            (pids['openrouter'], 'Claude Sonnet 4',      'anthropic/claude-sonnet-4','https://openrouter.ai/api/v1',                             'openrouter_api_key',    'text',     51),
            (pids['openrouter'], 'Claude 3 Opus',        'anthropic/claude-3-opus','https://openrouter.ai/api/v1',                              'openrouter_api_key',    'text',     52),
            (pids['openrouter'], 'Gemini 2.5 Pro',       'google/gemini-2.5-pro',  'https://openrouter.ai/api/v1',                              'openrouter_api_key',    'text',     53),
            (pids['openrouter'], 'Llama 4 Maverick',     'meta-llama/llama-4-maverick','https://openrouter.ai/api/v1',                           'openrouter_api_key',    'text',     54),
            # SiliconFlow
            (pids['siliconflow'], 'DeepSeek V3',        'deepseek-ai/DeepSeek-V3', 'https://api.siliconflow.cn/v1',                           'siliconflow_api_key',   'text',     55),
            (pids['siliconflow'], 'DeepSeek R1',        'deepseek-ai/DeepSeek-R1', 'https://api.siliconflow.cn/v1',                           'siliconflow_api_key',   'text',     56),
            (pids['siliconflow'], 'DeepSeek OCR',       'deepseek-ai/DeepSeek-OCR','https://api.siliconflow.cn/v1',                           'siliconflow_api_key',   'text',     57),
            (pids['siliconflow'], 'FLUX.1 Schnell',      'black-forest-labs/FLUX.1-schnell',       'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'image',    58),
            (pids['siliconflow'], 'FLUX.1 Pro',          'black-forest-labs/FLUX.1-pro',           'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'image',    59),
            (pids['siliconflow'], 'FLUX.1 Dev',          'black-forest-labs/FLUX.1-dev',           'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'image',    60),
            (pids['siliconflow'], 'Stable Diffusion 3.5','stabilityai/stable-diffusion-3.5-large', 'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'image',    61),
            (pids['siliconflow'], 'SDXL Base 1.0',       'stabilityai/stable-diffusion-xl-base-1.0','https://api.siliconflow.cn/v1',           'siliconflow_api_key',   'image',    62),
            (pids['siliconflow'], 'Qwen 2.5 72B',        'Qwen/Qwen2.5-72B-Instruct',       'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'text',     63),
            (pids['siliconflow'], 'Llama 3.3 70B',       'meta-llama/Llama-3.3-70B-Instruct','https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'text',     64),
            (pids['siliconflow'], 'DeepSeek R1 0528',    'deepseek-ai/DeepSeek-R1-0528',   'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'text',     65),
            (pids['siliconflow'], 'QwQ-32B',             'Qwen/QwQ-32B',                    'https://api.siliconflow.cn/v1',            'siliconflow_api_key',   'text',     66),
            # Google Gemini (OpenAI-compatible endpoint)
            (pids['gemini'],     'Gemini 3.0 Pro',       'gemini-3.0-pro',         'https://generativelanguage.googleapis.com/v1beta/openai/',     'gemini_api_key',        'text',     63),
            (pids['gemini'],     'Gemini 2.5 Pro',       'gemini-2.5-pro',         'https://generativelanguage.googleapis.com/v1beta/openai/',     'gemini_api_key',        'text',     64),
            (pids['gemini'],     'Gemini 2.5 Flash',     'gemini-2.5-flash',       'https://generativelanguage.googleapis.com/v1beta/openai/',     'gemini_api_key',        'text',     65),
            (pids['gemini'],     'Gemini 2.5 Flash Lite','gemini-2.5-flash-lite',  'https://generativelanguage.googleapis.com/v1beta/openai/',     'gemini_api_key',        'text',     66),
            (pids['gemini'],     'Gemini Embedding',     'text-embedding-004',     'https://generativelanguage.googleapis.com/v1beta/openai/',     'gemini_api_key',        'embedding',67),
            # xAI Grok
            (pids['grok'],       'Grok-3',               'grok-3',                 'https://api.x.ai/v1',                                         'xai_api_key',           'text',     70),
            (pids['grok'],       'Grok-3 Beta',          'grok-3-beta',            'https://api.x.ai/v1',                                         'xai_api_key',           'text',     71),
            (pids['grok'],       'Grok-2',               'grok-2-1212',            'https://api.x.ai/v1',                                         'xai_api_key',           'text',     72),
            # KIMI / Moonshot (OpenAI-compatible)
            (pids['kimi'],       'Moonshot v1 8K',      'moonshot-v1-8k',         'https://api.moonshot.cn/v1',                                  'kimi_api_key',          'text',     73),
            (pids['kimi'],       'Moonshot v1 32K',     'moonshot-v1-32k',        'https://api.moonshot.cn/v1',                                  'kimi_api_key',          'text',     74),
            (pids['kimi'],       'Moonshot v1 128K',    'moonshot-v1-128k',       'https://api.moonshot.cn/v1',                                  'kimi_api_key',          'text',     75),
            (pids['kimi'],       'K2',                   'kimi-k2',                'https://api.moonshot.cn/v1',                                  'kimi_api_key',          'text',     76),
            # Zhipu / ChatGLM (OpenAI-compatible)
            (pids['zhipu'],      'GLM-4',               'glm-4',                  'https://open.bigmodel.cn/api/paas/v4',                        'zhipu_api_key',         'text',     77),
            (pids['zhipu'],      'GLM-4 Flash',         'glm-4-flash',            'https://open.bigmodel.cn/api/paas/v4',                        'zhipu_api_key',         'text',     78),
            (pids['zhipu'],      'GLM-4 Plus',          'glm-4-plus',             'https://open.bigmodel.cn/api/paas/v4',                        'zhipu_api_key',         'text',     79),
            (pids['zhipu'],      'GLM-4V Plus',          'glm-4v-plus',            'https://open.bigmodel.cn/api/paas/v4',                        'zhipu_api_key',         'vision',   80),
            (pids['zhipu'],      'CogView-4',            'cogview-4',              'https://open.bigmodel.cn/api/paas/v4',                        'zhipu_api_key',         'image',    81),
            # Edge-TTS (free, no key needed)
            (pids['edge_tts'],   'Edge TTS Neural',      'edge-tts-neural',        '',                                                              '',                     'tts',      100),
        ]
        for pid, name, model, url, key_ref, caps, sort in model_seeds:
            m.execute(
                "INSERT INTO provider_models (provider_id, name, model_name, endpoint_url, api_key_ref, capabilities, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (provider_id, model_name) DO NOTHING",
                (pid, name, model, url, key_ref, caps, sort)
            )
        m.commit()
        # Backfill api_key_id: link provider_models → provider_api_keys via providers
        # 全新库守卫：api_key_id 由模块级补列（import 时表未建则 no-op）补不到，seed 后按需补列（幂等；旧库列已存在自动跳过）
        cols_pm = get_table_columns(m, 'provider_models')
        if 'api_key_id' not in cols_pm:
            m.execute('ALTER TABLE provider_models ADD COLUMN api_key_id BIGINT DEFAULT NULL REFERENCES provider_api_keys(id)')
        m.execute("""
            UPDATE provider_models pm
            SET api_key_id = pak.id
            FROM providers p, provider_api_keys pak
            WHERE pm.provider_id = p.id
              AND pak.provider = p.slug
              AND pm.api_key_id IS NULL
        """)
        m.commit()
        print('[Migration] Providers + provider_models seed data added')

    # ── Migration: add provider_model_id to agents table ──
    with get_db() as m:
        cols = get_table_columns(m, 'agents')
        if 'provider_model_id' not in cols:
            m.execute('ALTER TABLE agents ADD COLUMN provider_model_id BIGINT DEFAULT NULL')
            print('[Migration] Added agents.provider_model_id')
        # Migrate OLD model_provider_id → provider_model_id
        rows = m.execute(
            "SELECT id, model_provider_id FROM agents WHERE provider_model_id IS NULL AND model_provider_id IS NOT NULL"
        ).fetchall()
        for a in rows:
            m.execute("UPDATE agents SET provider_model_id = %s WHERE id = %s",
                      (a['model_provider_id'], a['id']))
        if rows:
            m.commit()
            print(f'[Migration] Migrated {len(rows)} agents from model_provider_id → provider_model_id')

        print('[Migration] verification_requests table created')

    # ── Migration: seed OpenRouter free models ──
    with get_db() as m:
        or_id = m.execute("SELECT id FROM providers WHERE slug='openrouter'").fetchone()
        if or_id:
            pid = or_id['id']
            or_free_models = [
                (pid, 'DeepSeek V4 Flash (免费)',   'deepseek/deepseek-v4-flash:free',                        'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 70),
                (pid, 'Llama 3.3 70B (免费)',       'meta-llama/llama-3.3-70b-instruct:free',               'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 71),
                (pid, 'Hermes 3 405B (免费)',        'nousresearch/hermes-3-llama-3.1-405b:free',            'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 72),
                (pid, 'Gemma 4 31B (免费)',          'google/gemma-4-31b-it:free',                            'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 73),
                (pid, 'Gemma 4 26B MoE (免费)',      'google/gemma-4-26b-a4b-it:free',                        'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 74),
                (pid, 'Qwen3 Next 80B (免费)',       'qwen/qwen3-next-80b-a3b-instruct:free',                 'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 75),
                (pid, 'Qwen3 Coder (免费)',          'qwen/qwen3-coder:free',                                 'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 76),
                (pid, 'Nemotron 3 Super 120B (免费)','nvidia/nemotron-3-super-120b-a12b:free',                'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 77),
                (pid, 'MiniMax M2.5 (免费)',         'minimax/minimax-m2.5:free',                             'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 78),
                (pid, 'GLM-4.5 Air (免费)',          'z-ai/glm-4.5-air:free',                                'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 79),
                (pid, 'GPT-OSS 120B (免费)',         'openai/gpt-oss-120b:free',                              'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 80),
                (pid, 'GPT-OSS 20B (免费)',          'openai/gpt-oss-20b:free',                               'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 81),
                (pid, 'CoBuddy 编程 (免费)',          'baidu/cobuddy:free',                                    'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 82),
                (pid, 'Trinity Large Thinking (免费)','arcee-ai/trinity-large-thinking:free',                'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 83),
                (pid, 'Nemotron Nano 30B (免费)',    'nvidia/nemotron-3-nano-30b-a3b:free',                  'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 84),
                (pid, 'Nemotron Nano Omni (免费)',   'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free',    'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 85),
                (pid, 'Nemotron Nano 9B V2 (免费)',  'nvidia/nemotron-nano-9b-v2:free',                       'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 86),
                (pid, 'Nemotron Nano 12B VL (免费)', 'nvidia/nemotron-nano-12b-v2-vl:free',                   'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 87),
                (pid, 'Llama 3.2 3B (免费)',         'meta-llama/llama-3.2-3b-instruct:free',                'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 88),
                (pid, 'Venice Uncensored (免费)',    'cognitivecomputations/dolphin-mistral-24b-venice-edition:free', 'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 89),
                (pid, 'LFM 2.5 Thinking (免费)',     'liquid/lfm-2.5-1.2b-thinking:free',                     'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 90),
                (pid, 'LFM 2.5 Instruct (免费)',     'liquid/lfm-2.5-1.2b-instruct:free',                     'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 91),
                (pid, 'Laguna XS.2 (免费)',          'poolside/laguna-xs.2:free',                             'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 92),
                (pid, 'Laguna M.1 (免费)',           'poolside/laguna-m.1:free',                              'https://openrouter.ai/api/v1', 'openrouter_api_key', 'text', 93),
            ]
            for pid_val, name, model, url, key_ref, caps, sort in or_free_models:
                m.execute(
                    "INSERT INTO provider_models (provider_id, name, model_name, endpoint_url, api_key_ref, capabilities, sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (provider_id, model_name) DO NOTHING",
                    (pid_val, name, model, url, key_ref, caps, sort)
                )
            m.commit()
            print('[Migration] OpenRouter free models seeded')

    # Check and add username_changed_at
    with get_db() as m:
        cols = get_table_columns(m, 'users')
        for col_name in ('username_changed_at',):
            if col_name not in cols:
                m.execute(f'ALTER TABLE users ADD COLUMN {col_name} TEXT')
                m.commit()
                print(f'[Migration] users.{col_name} added')

    # Migration: add social_links.platform column (2026-05-14)
    with get_db() as m:
        cols = get_table_columns(m, 'social_links')
        if 'platform' not in cols:
            m.execute("ALTER TABLE social_links ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
            m.commit()
            print('[Migration] social_links.platform added')

    # ── channel_configs: 频道管理（飞书/微信/QQ/钉钉）──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS channel_configs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                channel         TEXT NOT NULL UNIQUE,
                config_json     TEXT NOT NULL DEFAULT '{}',
                is_enabled      BIGINT DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        # seed feishu record if not exists
        existing = m.execute("SELECT id FROM channel_configs WHERE channel='feishu'").fetchone()
        if not existing:
            import os as _os
            m.execute(
                "INSERT INTO channel_configs (channel, config_json, is_enabled) VALUES ('feishu', %s, 1) ON CONFLICT (channel) DO NOTHING",
                ('{}',)
            )
            m.commit()
            print('[Migration] channel_configs table + feishu seed created')

        # seed wecom record if not exists
        existing_wecom = m.execute("SELECT id FROM channel_configs WHERE channel='wecom'").fetchone()
        if not existing_wecom:
            m.execute(
                "INSERT INTO channel_configs (channel, config_json, is_enabled) VALUES ('wecom', '{}', 1) ON CONFLICT (channel) DO NOTHING"
            )
            m.commit()
            print('[Migration] channel_configs wecom seed created')

        # seed qq record if not exists
        existing_qq = m.execute("SELECT id FROM channel_configs WHERE channel='qq'").fetchone()
        if not existing_qq:
            m.execute(
                "INSERT INTO channel_configs (channel, config_json, is_enabled) VALUES ('qq', '{}', 0) ON CONFLICT (channel) DO NOTHING"
            )
            m.commit()
            print('[Migration] channel_configs qq seed created')

        # seed dingtalk record if not exists
        existing_dingtalk = m.execute("SELECT id FROM channel_configs WHERE channel='dingtalk'").fetchone()
        if not existing_dingtalk:
            m.execute(
                "INSERT INTO channel_configs (channel, config_json, is_enabled) VALUES ('dingtalk', '{}', 0) ON CONFLICT (channel) DO NOTHING"
            )
            m.commit()
            print('[Migration] channel_configs dingtalk seed created')

    # ── Payment / Third-party config seeds (admin fills in credentials later) ──
    with get_db() as m:
        payment_seeds = [
            # 支付回调域名
            ('payment.notify_base',           '',    '支付回调通知域名 (如 https://your-domain.com)'),
            # 支付宝商城支付（payment_service.py 使用，无点前缀）
            ('alipay_app_id',                 '',    '支付宝 App ID（商城支付）'),
            ('alipay_private_key',            '',    '支付宝应用私钥 PKCS8（商城支付）'),
            ('alipay_public_key',             '',    '支付宝公钥（商城支付）'),
            # 微信支付
            ('wechat_app_id',                 '',    '微信支付 AppID（公众号/小程序 AppID）'),
            ('wechat_mchid',                  '',    '微信支付商户号'),
            ('wechat_api_v3_key',             '',    '微信支付 API v3 密钥'),
            ('wechat_cert_serial',            '',    '微信支付证书序列号'),
            ('wechat_plan_id',                '',    '微信支付扣费计划ID'),
            # 快递鸟物流（已由 plugins/logistics 插件独立管理，保留 key 仅供历史兼容）
            ('kdniao_eid',                    '',    '【已迁移至插件】快递鸟商户ID'),
            ('kdniao_api_key',                '',    '【已迁移至插件】快递鸟 API Key'),
        ]
        for key, value, desc in payment_seeds:
            m.execute(
                "INSERT INTO system_config (key, value, description) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING",
                (key, value, desc)
            )
        m.commit()
        print('[Migration] Payment/third-party config seeds added')

    # ── Shop AI 商城商品优化配置 ──
    with get_db() as m:
        shop_ai_seeds = [
            ('shop_ai_provider',                'deepseek',     '商城AI商品优化 — 供应商 (deepseek/dashscope/openai/openrouter/siliconflow/ollama)'),
            ('shop_ai_model',                   '','商城AI商品优化 — 模型名（来自 AI Hub，留空自动使用默认）'),
        ]
        for key, value, desc in shop_ai_seeds:
            m.execute(
                "INSERT INTO system_config (key, value, description) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING",
                (key, value, desc)
            )
        m.commit()
        print('[Migration] Shop AI config seeds added')

    # ── cluster_services: 站群服务管理 ──
    with get_db() as m2:
        m2.execute("""
            CREATE TABLE IF NOT EXISTS cluster_services (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                service_name    TEXT NOT NULL UNIQUE,
                display_name    TEXT NOT NULL,
                domain          TEXT NOT NULL,
                port            BIGINT NOT NULL,
                health_url      TEXT DEFAULT '/health',
                manager_type    TEXT NOT NULL DEFAULT 'tmux',
                manager_name    TEXT NOT NULL,
                workdir         TEXT,
                start_cmd       TEXT,
                sort_order      BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m2.commit()
    # ── Notification System: templates + logs tables ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS notification_templates (
                id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                event_type          TEXT NOT NULL UNIQUE,
                title_template      TEXT NOT NULL,
                content_template    TEXT NOT NULL,
                link_url_template   TEXT DEFAULT '',
                type                TEXT NOT NULL DEFAULT 'system',
                is_active           BIGINT DEFAULT 1,
                sort_order          BIGINT DEFAULT 0,
                created_at          TIMESTAMP DEFAULT NOW(),
                updated_at          TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                template_id     BIGINT DEFAULT NULL,
                user_id         BIGINT REFERENCES users(id),
                event_type      TEXT DEFAULT '',
                notification_id BIGINT DEFAULT NULL,
                result          TEXT DEFAULT 'success',
                error_msg       TEXT DEFAULT '',
                sent_at         TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("CREATE INDEX IF NOT EXISTS idx_notif_logs_user ON notification_logs(user_id)")
        m.execute("CREATE INDEX IF NOT EXISTS idx_notif_logs_template ON notification_logs(template_id)")
        m.commit()
    # Migration: add read_at + extra_data to user_notifications
    with get_db() as m:
        cols = get_table_columns(m, 'user_notifications')
        if 'read_at' not in cols:
            m.execute("ALTER TABLE user_notifications ADD COLUMN read_at TEXT DEFAULT NULL")
        if 'extra_data' not in cols:
            m.execute("ALTER TABLE user_notifications ADD COLUMN extra_data TEXT DEFAULT '{}'")
        m.commit()
        print('[Migration] user_notifications: read_at + extra_data added')

    # ── Migration: completion_percentage on users ──
    with get_db() as m:
        cols = get_table_columns(m, 'users')
        if 'completion_percentage' not in cols:
            m.execute("ALTER TABLE users ADD COLUMN completion_percentage BIGINT DEFAULT 0")
        if 'completion_last_updated' not in cols:
            m.execute("ALTER TABLE users ADD COLUMN completion_last_updated TEXT")
        m.commit()
        print('[Migration] users: completion_percentage + completion_last_updated added')

    # ── Reward rules + claims tables ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS reward_rules (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL,
                condition_key   TEXT NOT NULL,
                condition_value TEXT NOT NULL,
                reward_type     TEXT NOT NULL DEFAULT 'coupon',
                reward_id       BIGINT DEFAULT NULL,
                reward_name     TEXT DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                is_active       BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS reward_claims (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id     BIGINT REFERENCES users(id),
                rule_id     BIGINT NOT NULL,
                claimed_at  TIMESTAMP DEFAULT NOW(),
                coupon_id   BIGINT DEFAULT NULL,
                UNIQUE(user_id, rule_id)
            )
        """)
        m.execute("CREATE INDEX IF NOT EXISTS idx_reward_claims_user ON reward_claims(user_id)")
        m.execute("CREATE INDEX IF NOT EXISTS idx_reward_claims_rule ON reward_claims(rule_id)")
        m.commit()
        print('[Migration] reward_rules + reward_claims tables created')

    # ── article_comments table (for comments.py) ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS article_comments (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                post_id         BIGINT NOT NULL REFERENCES cms_posts(id),
                parent_id       BIGINT,
                nickname        TEXT NOT NULL DEFAULT 'Anonymous',
                content         TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected')),
                ai_review       TEXT DEFAULT '',
                ai_score        BIGINT DEFAULT 0,
                ip_address      TEXT DEFAULT '',
                reviewed_by     BIGINT,
                reviewed_at     TIMESTAMP,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("CREATE INDEX IF NOT EXISTS idx_article_comments_post ON article_comments(post_id)")
        m.execute("CREATE INDEX IF NOT EXISTS idx_article_comments_status ON article_comments(status)")
        m.commit()
        print('[Migration] article_comments table created')

    # ── Interests + user_interests tables ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS interests (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                category    TEXT NOT NULL,
                sort_order  BIGINT DEFAULT 0,
                is_hot      BIGINT DEFAULT 0,
                is_active   BIGINT DEFAULT 1
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS user_interests (
                user_id     BIGINT NOT NULL,
                interest_id BIGINT NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, interest_id),
                FOREIGN KEY (interest_id) REFERENCES interests(id) ON DELETE CASCADE
            )
        """)
        m.execute("CREATE INDEX IF NOT EXISTS idx_user_interests_user ON user_interests(user_id)")
        m.execute("CREATE INDEX IF NOT EXISTS idx_interests_category ON interests(category, sort_order)")
        m.commit()
        print('[Migration] interests + user_interests tables created')

    # ── social_media_links + header_nav + footer_* + partner_links ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS social_media_links (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                platform_name   TEXT NOT NULL DEFAULT '',
                icon_type       TEXT NOT NULL DEFAULT 'fontawesome',
                icon_value      TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                display_order   BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                hover_text      TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS header_nav (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site            TEXT NOT NULL DEFAULT 'platform',
                title           TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS footer_links (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                section         TEXT NOT NULL DEFAULT '',
                title           TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS footer_nav (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                title           TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS footer_articles (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                title           TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS partner_links (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                icon_url        TEXT DEFAULT '',
                sort_order      BIGINT DEFAULT 0,
                is_enabled      BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.commit()
        print('[Migration] social_media_links + header_nav + footer_* + partner_links tables created')

    # ── regions: 行政区划表（中国省市三级联动）──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS regions (
                code            BIGINT PRIMARY KEY,
                name            TEXT NOT NULL,
                level           BIGINT DEFAULT 0,
                parent_code     BIGINT DEFAULT 0,
                full_name       TEXT DEFAULT '',
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        m.commit()
        # 检查是否需要 seed（仅空表时写入基础层级）
        empty = m.execute("SELECT count(*) FROM regions").fetchone()['count']
        if empty == 0:
            # 省/自治区/直辖市 level-1（简化 seed，仅基础数据）
            import json
            base_regions = [
                (110000, '北京市', 1, 100000, '中国/北京市'),
                (120000, '天津市', 1, 100000, '中国/天津市'),
                (130000, '河北省', 1, 100000, '中国/河北省'),
                (140000, '山西省', 1, 100000, '中国/山西省'),
                (150000, '内蒙古自治区', 1, 100000, '中国/内蒙古自治区'),
                (210000, '辽宁省', 1, 100000, '中国/辽宁省'),
                (220000, '吉林省', 1, 100000, '中国/吉林省'),
                (230000, '黑龙江省', 1, 100000, '中国/黑龙江省'),
                (310000, '上海市', 1, 100000, '中国/上海市'),
                (320000, '江苏省', 1, 100000, '中国/江苏省'),
                (330000, '浙江省', 1, 100000, '中国/浙江省'),
                (340000, '安徽省', 1, 100000, '中国/安徽省'),
                (350000, '福建省', 1, 100000, '中国/福建省'),
                (360000, '江西省', 1, 100000, '中国/江西省'),
                (370000, '山东省', 1, 100000, '中国/山东省'),
                (410000, '河南省', 1, 100000, '中国/河南省'),
                (420000, '湖北省', 1, 100000, '中国/湖北省'),
                (430000, '湖南省', 1, 100000, '中国/湖南省'),
                (440000, '广东省', 1, 100000, '中国/广东省'),
                (450000, '广西壮族自治区', 1, 100000, '中国/广西壮族自治区'),
                (460000, '海南省', 1, 100000, '中国/海南省'),
                (500000, '重庆市', 1, 100000, '中国/重庆市'),
                (510000, '四川省', 1, 100000, '中国/四川省'),
                (520000, '贵州省', 1, 100000, '中国/贵州省'),
                (530000, '云南省', 1, 100000, '中国/云南省'),
                (540000, '西藏自治区', 1, 100000, '中国/西藏自治区'),
                (610000, '陕西省', 1, 100000, '中国/陕西省'),
                (620000, '甘肃省', 1, 100000, '中国/甘肃省'),
                (630000, '青海省', 1, 100000, '中国/青海省'),
                (640000, '宁夏回族自治区', 1, 100000, '中国/宁夏回族自治区'),
                (650000, '新疆维吾尔自治区', 1, 100000, '中国/新疆维吾尔自治区'),
                (710000, '台湾省', 1, 100000, '中国/台湾省'),
                (810000, '香港特别行政区', 1, 100000, '中国/香港特别行政区'),
                (820000, '澳门特别行政区', 1, 100000, '中国/澳门特别行政区'),
            ]
            for b in base_regions:
                m.execute("INSERT INTO regions (code, name, level, parent_code, full_name) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING", b)
            m.commit()
            print(f'[Migration] regions: {len(base_regions)} level-1 regions seeded')
        else:
            print(f'[Migration] regions: {empty} rows already exist, skipping seed')

    # ── Seed default interest tags ──
    with get_db() as m:
        existing = m.execute("SELECT COUNT(*) FROM interests").fetchone()['count']
        if existing < 10:
            tags = _get_default_interests()
            m.executemany(
                "INSERT INTO interests (name, category, sort_order, is_hot) VALUES (%s,%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                tags
            )
            m.commit()
            print(f'[Migration] {len(tags)} interest tags seeded')

    # ── Seed notification templates ──
    with get_db() as m:
        templates = [
            ('user.realname_verified',      '实名认证通过',   '恭喜您已通过实名认证，解锁全部功能权益。',                                                          '',                   'reward', 1, 1),
            ('user.profile_completion.100',  '资料完成度100%', '您已完成全部个人资料填写，获得专属福利！',                                                            '',                   'reward', 2, 2),
            ('user.phone_verified',          '手机验证成功',   '您已成功验证手机号，账户安全等级已提升。',                                                            '',                   'system', 3, 3),
            ('reward.issued',               '获得奖励',       '恭喜您获得 {reward_name}！请前往优惠券中心查看。',                                               '',                   'reward', 4, 4),
            ('referral.referee_registered', '邀请成功',       '恭喜邀请成功！您的好友 {friend_name} 已注册，奖励已发放。',                                      '',                   'reward', 5, 5),
            ('referral.referee_completed_action', '好友完成首单', '您的好友 {friend_name} 已完成首次操作，您的推广奖励已到账。',                                '',                   'reward', 6, 6),
            ('coupon.expiring',             '优惠券即将过期', '您有一张 {coupon_name} 即将在 {expire_days} 天后过期，请尽快使用。',                         '',                   'promo',  7, 7),
        ]
        for t in templates:
            existing = m.execute("SELECT id FROM notification_templates WHERE event_type = %s", (t[0],)).fetchone()
            if not existing:
                m.execute(
                    "INSERT INTO notification_templates (event_type, title_template, content_template, link_url_template, type, sort_order, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (event_type) DO NOTHING",
                    t
                )
        m.commit()
        print('[Migration] notification templates seeded')

        

    # ── Migration: drop volcengine voice/video tables (volcengine removed 2026-07-21) ──
    with get_db() as m:
        m.execute('DROP TABLE IF EXISTS voice_templates CASCADE')
        m.execute('DROP TABLE IF EXISTS video_tasks CASCADE')
        m.commit()
        print('[Migration] voice_templates + video_tasks tables dropped (volcengine removed)')

    # ── Migration: remove volcengine provider & its models (2026-07-21) ──
    with get_db() as m:
        m.execute("DELETE FROM provider_models WHERE provider_id = (SELECT id FROM providers WHERE slug = 'volcengine')")
        m.execute("DELETE FROM providers WHERE slug = 'volcengine'")
        m.commit()
        print('[Migration] volcengine provider + provider_models removed')

    # ── Migration: media_files table（本地媒体库 — 2026-05-24）──
    with get_db() as m:
        m.execute('''CREATE TABLE IF NOT EXISTS media_files (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            filename        TEXT NOT NULL,
            original_name   TEXT NOT NULL,
            mime_type       TEXT NOT NULL DEFAULT 'application/octet-stream',
            file_size       BIGINT DEFAULT 0,
            file_path       TEXT NOT NULL,
            thumb_path      TEXT DEFAULT '',
            push_status     TEXT DEFAULT 'none',
            push_target     TEXT DEFAULT '',
            pushed_at       TEXT DEFAULT NULL,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )''')
        m.execute('CREATE INDEX IF NOT EXISTS idx_mf_push_status ON media_files(push_status)')
        m.execute('CREATE INDEX IF NOT EXISTS idx_mf_created ON media_files(created_at)')
        m.commit()
        print('[Migration] media_files table created')
    # ── Migration: knowledge_blocks table（RAG知识库 — 2026-06-10）──
    with get_db() as m:
        m.execute("""CREATE TABLE IF NOT EXISTS knowledge_blocks (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            content         TEXT NOT NULL,
            keywords        TEXT DEFAULT '',
            category        TEXT DEFAULT '',
            priority        BIGINT DEFAULT 0,
            created_at      TEXT DEFAULT NOW()
        )""")
        m.execute('CREATE INDEX IF NOT EXISTS idx_kb_category ON knowledge_blocks(category)')
        # ── Migration: knowledge_blocks scope/owner_id 双域隔离 (2026-07-24) ──
        kb_cols = get_table_columns(m, 'knowledge_blocks')
        if 'scope' not in kb_cols:
            m.execute("ALTER TABLE knowledge_blocks ADD COLUMN scope VARCHAR(20) DEFAULT 'system'")
            print('[Migration] knowledge_blocks.scope added')
        if 'owner_id' not in kb_cols:
            m.execute("ALTER TABLE knowledge_blocks ADD COLUMN owner_id BIGINT DEFAULT NULL")
            print('[Migration] knowledge_blocks.owner_id added')
        m.execute('CREATE INDEX IF NOT EXISTS idx_kb_scope ON knowledge_blocks(scope)')
        m.execute('CREATE INDEX IF NOT EXISTS idx_kb_owner ON knowledge_blocks(owner_id)')
        # Backfill existing data: distinguish system KB from user KB by id prefix
        m.execute("UPDATE knowledge_blocks SET scope='user', owner_id=NULL WHERE id LIKE 'kb_company_%' OR id LIKE 'kb_product_%' OR id LIKE 'kb_faq_faq_%' OR id LIKE 'kb_faq_whitepaper%' OR (id LIKE 'kb_faq_%' AND id NOT LIKE 'kb_faq_faq_%')")
        m.execute("UPDATE knowledge_blocks SET scope='user', owner_id=NULL WHERE id LIKE 'kb_cleaner_%'")
        # Backfill only when the 'source' column exists (added by the 2026-07-18 migration)
        kb_cols_after = get_table_columns(m, 'knowledge_blocks')
        if 'source' in kb_cols_after:
            m.execute("UPDATE knowledge_blocks SET scope='user', owner_id=NULL WHERE scope IS NULL AND source='manual'")
            m.execute("UPDATE knowledge_blocks SET scope='user', owner_id=NULL WHERE scope IS NULL AND source IN ('auto','matrix')")
        print('[Migration] knowledge_blocks scope/owner_id migration completed')
        # Seed knowledge blocks from mini-program
        row = m.execute("SELECT COUNT(*) as c FROM knowledge_blocks").fetchone()
        if row['c'] == 0:
            kb_seeds = [
                ('kb_company_001','公司基本信息','Demo Company，位于示例地址。服务热线：400-000-0000，工作时间：周一至周五 9:00-18:00。','公司,地址,电话,邮箱,联系方式,工作时间','company',10),
                ('kb_company_002','公司定位与愿景','本平台由专业团队研发，定位为AI驱动的企业智能运营平台。公司愿景是通过AI技术降低企业运营门槛，助力中小企业数字化转型。','定位,愿景,使命,AI驱动,企业运营,数字化转型,中小企业','company',8),
                ('kb_product_001','平台概述','企业智能运营平台，核心功能包括：AI智能体社区、自动生成文案/图片/SEO、可视化搭建工作流、多端适配等。','平台,概述,功能,智能体,工作流,多端适配','product',10),
                ('kb_product_002','AI智能体社区','AI智能体社区是核心模块，内置多种AI助手：SEO优化助手、文案生成助手、图片设计助手、数据分析助手、客服助手等。每个智能体专注于特定任务。','智能体,AI助手,SEO,文案,图片,数据分析,客服,协作','product',9),
                ('kb_product_003','AI内容工厂','AI内容工厂可自动生成运营所需的各类内容：产品描述文案、企业介绍、新闻资讯、SEO优化文章、营销配图、Banner广告图等。支持批量生成和人工微调。','内容工厂,文案生成,图片生成,SEO文章,批量生成,Banner','product',9),
                ('kb_product_004','智能工作流引擎','智能工作流引擎提供可视化拖拽式页面搭建体验，无需编程即可完成页面配置。支持组件库、页面模板、样式自定义、实时预览等功能。','工作流,拖拽,可视化,组件,模板,预览','product',9),
                ('kb_product_005','多端适配能力','支持一次搭建、多端发布：PC网站、移动H5、微信小程序、抖音小程序、支付宝小程序等。自动适配不同终端的屏幕尺寸和交互方式。','多端,适配,PC,H5,小程序,响应式,跨平台','product',8),
                ('kb_price_001','价格体系概述','平台提供灵活的定价方案：基础版适合个人/初创企业，专业版适合中小企业，企业版适合大型企业。具体价格请咨询客服获取最新报价。','价格,多少钱,费用,报价,定价,收费,套餐,基础版,专业版,企业版','price',10),
                ('kb_price_002','基础版方案','基础版适合个人或初创企业，包含基础功能搭建、页面上限、基础SEO优化、响应式适配。价格亲民，是入门的最佳选择。','基础版,入门,个人,初创,便宜,低价','price',8),
                ('kb_price_003','专业版方案','专业版适合中小企业，包含企业级功能搭建、AI内容工厂、SEO深度优化、数据分析看板、多端适配。性价比最高，适合有线上运营需求的企业。','专业版,中小企业,性价比,营销,SEO深度','price',8),
                ('kb_price_004','企业版方案','企业版适合大型企业/集团，包含全部功能、定制化开发、专属客服、API接口对接、技术支持。适合有复杂定制需求的大型组织。','企业版,大型企业,定制,API,专属客服','price',8),
                ('kb_tech_001','AI技术优势','平台采用最新大语言模型技术，结合自研的运营领域知识图谱，实现智能化的系统配置。AI可理解用户需求描述，自动推荐合适的方案、布局和内容配置。','AI技术,大模型,知识图谱,智能推荐,技术优势','tech',8),
                ('kb_tech_002','部署说明','平台支持多种部署方式，签约后客户可获得完整的部署方案。交付物包含前端代码、后端接口、数据库脚本等，客户可自行部署和二次开发。','部署,交付,代码,二次开发,前端,后端,数据库','tech',9),
                ('kb_tech_003','安全与性能','平台采用HTTPS加密传输、数据备份、DDoS防护等安全措施。网站性能方面：CDN加速、图片懒加载、代码压缩、缓存策略等，确保网站加载速度快、运行稳定。','安全,性能,HTTPS,备份,CDN,加速,加载速度,稳定,防护','tech',7),
                ('kb_service_001','合作流程','合作流程：1.需求沟通（了解您的业务需求和预算）；2.方案定制（AI生成个性化方案）；3.合同签订（明确交付内容和时间节点）；4.开发搭建（AI+人工协作高效交付）；5.验收上线（测试通过后正式发布）；6.售后维护（持续技术支持）。','流程,合作,步骤,需求,方案,合同,开发,验收,售后,维护','service',9),
                ('kb_service_002','售后服务','平台提供完善的售后服务：免费维护期、7×24小时技术支持、定期系统更新、紧急故障2小时响应、免费培训。维护期后可续费延长。','售后,维护,技术支持,更新,故障,培训,续费,服务','service',8),
                ('kb_service_003','行业解决方案','针对不同行业提供专属解决方案：电商零售（商品展示+在线支付）、教育培训（课程展示+在线报名）、餐饮美食（菜单展示+外卖对接）、企业服务（品牌展示+线索收集）、房地产（楼盘展示+VR看房）等。','行业,解决方案,电商,教育,餐饮,企业服务,房地产,VR看房,外卖','service',8),
                ('kb_faq_001','搭建需要多长时间','展示型页面最快1天即可上线，企业官网通常3-5个工作日，含综合方案约7-10个工作日。具体时间取决于需求复杂度和定制化程度。','时间,多久,周期,上线,工作日,快速,几天','faq',9),
                ('kb_faq_002','是否需要技术基础','平台采用可视化拖拽操作，无需编程基础即可使用。AI助手会引导您完成每一步操作。如果有特殊定制需求，技术支持团队会提供专业支持。','技术基础,编程,代码,不会,简单,操作,难不难,容易','faq',9),
                ('kb_faq_003','是否支持SEO优化','平台内置SEO优化功能，AI可自动生成TDK（标题、描述、关键词）、优化页面结构、生成sitemap、配置301重定向等。同时提供SEO分析报告和改进建议。','SEO,优化,搜索引擎,排名,TDK,sitemap,百度,Google','faq',8),
                ('kb_faq_004','域名和服务器说明','平台可协助客户完成域名注册和服务器配置。客户可使用自有域名，也可通过平台代购。服务器采用云部署方案，自动扩容，保障稳定运行。域名和服务器费用不包含在套餐内。','域名,服务器,云部署,扩容,注册,代购,备案','faq',7),
            ]
            for s in kb_seeds:
                m.execute("INSERT INTO knowledge_blocks (id,title,content,keywords,category,priority,scope,owner_id) VALUES (%s,%s,%s,%s,%s,%s,'user',NULL) ON CONFLICT (id) DO NOTHING", s)
            m.commit()
            print(f'[Migration] knowledge_blocks seeded: {len(kb_seeds)} blocks')

    # ── Migration: seed FAQ and white paper from community/ (2026-06-11) ──
    with get_db() as ms:
        # Only seed if fewer than 25 entries (commercial FAQ not yet seeded)
        cnt = ms.execute("SELECT COUNT(*) as c FROM knowledge_blocks").fetchone()
        if cnt['c'] < 25:
            faq_seeds_data = [
                ('kb_faq_faq_p1', '你们的产品有哪些功能？', 'VeroRun(RuiCe AI)是生成式AI平台，支持智能对话（多模型）、AI建站、AI内容生成、数据清洗、知识库管理等。具体可查看官网产品页面或试用体验。', '功能,产品,能力,特性', 'faq', 6),
                ('kb_faq_faq_p2', '支持哪些AI模型？', '我们通过 Agent Matrix 体系支持多种AI模型，包括 DeepSeek（推荐）、阿里通义千问、以及 OpenAI 兼容接口。模型选择可在系统设置中配置。', '模型,AI,DeepSeek,千问,OpenAI', 'tech', 6),
                ('kb_faq_faq_p3', '怎么收费？有哪些套餐？', '我们提供基础版(¥299/月)、专业版(¥899/月)、企业版(¥2999/月)三档套餐。每个套餐赠送不同额度的API调用量。具体价格请查看官网定价页面或联系商务获取最新报价。', '价格,多少钱,费用,报价,收费,套餐,月付', 'price', 7),
                ('kb_faq_faq_p4', '有免费试用吗？', '是的！新用户注册即享免费体验额度。无需绑定支付方式即可试用。基础版可免费使用部分核心功能。', '免费,试用,体验,测试', 'price', 7),
                ('kb_faq_faq_p5', '如何注册账号？', '访问官网点击注册，填写用户名和密码即可完成注册。注册后即可使用免费额度体验平台功能。', '注册,账号,登录,开通', 'service', 7),
                ('kb_faq_faq_p6', '支持API接入吗？', '支持标准 OpenAI 兼容 API 接口。在后台管理控制台中生成 API Key 后即可调用。', 'API,接入,对接,接口,开发', 'tech', 6),
                ('kb_faq_faq_a1', '回复很慢怎么办？', '回复速度受模型负载和网络影响。建议：1）检查网络连接 2）避开高峰期使用 3）尝试切换模型。如持续异常，请提交工单。', '慢,卡,延迟,响应速度', 'service', 5),
                ('kb_faq_faq_a2', '对话记录在哪里查看？', '登录用户控制台，在「对话历史」中可以查看和搜索所有历史对话记录。', '记录,历史,对话,查看,搜索', 'service', 5),
                ('kb_faq_faq_a4', '忘记密码了怎么办？', '在登录页面点击「忘记密码」，输入注册时绑定的信息即可重置密码。如未绑定，请联系客服协助处理。', '密码,忘记,重置,找回', 'service', 6),
                ('kb_faq_faq_o2', '数据安全吗？隐私如何保护？', '我们高度重视数据安全：对话内容加密传输和存储，不会用于模型训练，用户可随时管理自己的数据。详细请查看官网法律声明。', '安全,隐私,数据,加密,保护', 'company', 7),
                ('kb_faq_whitepaper', 'VeroRun白皮书', 'VeroRun(RuiCe AI)是新一代AI驱动的智能建站与企业数字化平台。系统集成了AI聊天机器人、知识库管理（RAG）、数据清洗、内容工厂、CMS门户、订阅计费、多站点管理等完整功能。核心优势：AI原生架构、一体化SSO、开箱即用支付、强大的后台管理和灵活的主题系统。', '白皮书,产品介绍,技术架构,AI建站,功能概述', 'company', 10),
                ('kb_faq_whitepaper_tech', '技术架构说明', '系统采用Python 3.12 + Flask多服务微架构，SQLite (WAL模式)数据库，Vanilla JS SPA前端。支持SSO统一登录、多种支付网关、SSE流式对话、RAG知识库检索、Agent矩阵智能体编排等核心技术。', '技术,架构,Flask,Python,SSO,支付', 'tech', 8),
            ]
            for s in faq_seeds_data:
                ms.execute("INSERT INTO knowledge_blocks (id,title,content,keywords,category,priority,scope,owner_id) VALUES (%s,%s,%s,%s,%s,%s,'user',NULL) ON CONFLICT (id) DO NOTHING", s)
            ms.commit()
            print(f'[Migration] FAQ & whitepaper seeded: {len(faq_seeds_data)} blocks')

    # ── Migration: knowledge_queue（数据清洗 — 2026-06-10）──
    with get_db() as m:
        m.execute('''CREATE TABLE IF NOT EXISTS knowledge_queue (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source          TEXT DEFAULT 'manual',
            raw_content     TEXT NOT NULL,
            status          TEXT DEFAULT 'pending',
            cleaned_id      TEXT,
            error_msg       TEXT DEFAULT '',
            admin_id        BIGINT DEFAULT 0,
            created_at      TEXT DEFAULT NOW()
        )''')
        m.execute('CREATE INDEX IF NOT EXISTS idx_kq_status ON knowledge_queue(status)')
        m.commit()
        print('[Migration] knowledge_queue table created')

    # ── shop tables 已迁移至独立 shop.db（init_shop_db）──

    # ── Migration: add receiver fields to order_items (shop.db) ──
    try:
        with get_db() as shop_conn:
            for col in ['receiver_name', 'receiver_phone', 'receiver_address']:
                try:
                    shop_conn.execute(f"ALTER TABLE shop.order_items ADD COLUMN {col} TEXT DEFAULT ''")
                except Exception:
                    pass  # already exists
            shop_conn.commit()
    except Exception:
        pass  # shop.db may not exist yet (first run)

    # ── Migration: order_items column additions (now in shop.db) ──
    # All order_items column migrations are handled by init_shop_db() DDL.
    # If any column is missing (production DB layout differs), run via shop. prefix.

    # ── Migration: seed extra themes (light/nature/warm/ocean) ──
    with get_db() as m:
        theme_seeds = [
            ('light', '纯净白', '1.0.0', '',
             '纯净白色风格 — 适合教育、咨询、法律服务。干净、通透、可信赖。',
             'education', '["light","clean","professional","education"]'),
            ('nature', '自然绿', '1.0.0', '',
             '自然绿色风格 — 适合餐饮、健康、农业、环保。清新、有机、生命力。',
             'food', '["green","nature","organic","food","health"]'),
            ('warm', '暖橙', '1.0.0', '',
             '暖橙色风格 — 适合零售、生活服务、美容、家居。温馨、亲切。',
             'retail', '["warm","orange","retail","lifestyle"]'),
            ('ocean', '深海蓝', '1.0.0', '',
             '深海蓝色风格 — 适合企业、制造、物流、金融。沉稳、专业。',
             'enterprise', '["dark","blue","enterprise","manufacturing"]'),
        ]
        for slug, name, ver, author, desc, industry, tags in theme_seeds:
            m.execute(
                "INSERT INTO themes (slug, name, version, author, description, industry, tags, config_json, dir_name) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (slug) DO NOTHING",
                (slug, name, ver, author, desc, industry, tags,
                 '{"name":"' + name + '","slug":"' + slug + '","version":"' + ver + '","builtin":false}', slug)
            )
        m.commit()
        print(f'[Migration] seed themes: {len(theme_seeds)} themes added')

    # ── Migration: brand_settings software_name + software_slogan ──
    with get_db() as m:
        cols = get_table_columns(m, 'brand_settings')
        if 'software_name' not in cols:
            m.execute("ALTER TABLE brand_settings ADD COLUMN software_name TEXT NOT NULL DEFAULT 'VeroRun 维洛智能'")
            m.commit()
            print('[Migration] brand_settings.software_name added')
        if 'software_slogan' not in cols:
            m.execute("ALTER TABLE brand_settings ADD COLUMN software_slogan TEXT NOT NULL DEFAULT 'Multi-Agent AI Operating System / 多智能体驱动的AI内容与商业枢纽'")
            m.commit()
            print('[Migration] brand_settings.software_slogan added')

    # ── Migration: tm_brand_settings site_name_cn → VeroRun ──
    with get_db() as m:
        try:
            row = m.execute("SELECT site_name_cn FROM tm_brand_settings WHERE id=1").fetchone()
            if row and row["site_name_cn"] == 'TradeMind':
                m.execute("UPDATE tm_brand_settings SET site_name_cn='VeroRun' WHERE id=1")
                m.commit()
                print("[Migration] tm_brand_settings.site_name_cn updated to VeroRun")
        except Exception:
            m.rollback()  # tm_brand_settings table no longer exists



    # ── Migration: drop cluster_services (2026-07-06) 合并到 site_domains ──
    with get_db() as m:
        m.execute("DROP TABLE IF EXISTS cluster_services")
        m.commit()
        print('[Migration] ✅ cluster_services table dropped (merged into site_domains)')

    # ── Migration: 合并 service_plans → subscription_plans（订阅SaaS归类）已随订阅解耦移除（P3）──
    # subscription_plans / subscription_orders 表已下线，订阅数据由插件 subscription schema 接管。

    # ── 抖音小程序支持：chat_messages + mp_profiles (2026-06-11) ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                openid      TEXT PRIMARY KEY,
                messages    TEXT DEFAULT '[]',
                created_at  TEXT,
                updated_at  TEXT
            )
        """)
        m.execute("""
            CREATE TABLE IF NOT EXISTS mp_profiles (
                openid      TEXT PRIMARY KEY,
                profile     TEXT DEFAULT '{}',
                summary     TEXT DEFAULT '',
                visit_count BIGINT DEFAULT 0,
                created_at  TEXT,
                updated_at  TEXT
            )
        """)
        m.commit()
        print('[Migration] chat_messages + mp_profiles tables created')

    # 迁移：为 mp_profiles 表添加 visit_count 字段
    try:
        with get_db() as m:
            m.execute("ALTER TABLE mp_profiles ADD COLUMN visit_count BIGINT DEFAULT 0")
    except Exception as e:
        import logging
        logging.debug(f"[Migration] mp_profiles visit_count column may already exist: {e}")

    # 迁移：为 chat_messages 表添加 platform + platform_user_id 字段 (2026-07-13)
    try:
        with get_db() as m:
            m.execute("ALTER TABLE chat_messages ADD COLUMN platform TEXT DEFAULT 'website'")
    except Exception as e:
        import logging
        logging.debug(f"[Migration] chat_messages platform column may already exist: {e}")
    try:
        with get_db() as m:
            m.execute("ALTER TABLE chat_messages ADD COLUMN platform_user_id TEXT DEFAULT ''")
    except Exception as e:
        import logging
        logging.debug(f"[Migration] chat_messages platform_user_id column may already exist: {e}")
    try:
        with get_db() as m:
            m.execute("CREATE INDEX IF NOT EXISTS idx_chat_platform ON chat_messages(platform, platform_user_id)")
    except Exception as e:
        import logging
        logging.debug(f"[Migration] chat_messages platform index may already exist: {e}")

    # ── oauth_providers 回归主库 public schema（§12.10，由 plugins/oauth_config 幂等建表） ──

    # ── products.images / categories / product_specs / product_skus / carts.sku_id ──
    # All handled by init_shop_db() with full column set.

    # ── Migration: 独立部署套餐 subscription_plans 已随订阅解耦移除（P3）──

    # ── pricing_rules / order_items payment+shipping / express_companies ──
    # All handled by init_shop_db() with full column set.

    # ── Migration: invoices 发票系统 已随订阅解耦移除（P3）──

    # ── Migration: chatbot_sessions AI 顾问对话元数据 (2026-07-12) ──
    with get_db() as m:
        m.execute("""
            CREATE TABLE IF NOT EXISTS chatbot_sessions (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                session_id  TEXT NOT NULL,
                user_query  TEXT DEFAULT '',
                ai_reply    TEXT DEFAULT '',
                escalated   BIGINT DEFAULT 0,
                csat_score  BIGINT DEFAULT 0,
                source      TEXT DEFAULT 'chatbot',
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute('CREATE INDEX IF NOT EXISTS idx_cs_created ON chatbot_sessions(created_at)')
        m.execute('CREATE INDEX IF NOT EXISTS idx_cs_session ON chatbot_sessions(session_id)')
        m.commit()
        print('[Migration] chatbot_sessions table created')

    # ── Migration: chatbot_sessions intent/sentiment 字段 (2026-07-12) ──
    with get_db() as m:
        existing = get_table_columns(m, 'chatbot_sessions')
        for col, col_def in {'intent': "intent TEXT DEFAULT ''",
                             'sentiment': "sentiment TEXT DEFAULT ''"}.items():
            if col not in existing:
                try:
                    m.execute(f"ALTER TABLE chatbot_sessions ADD COLUMN {col_def}")
                    print(f'[Migration] chatbot_sessions.{col} added')
                except Exception as e:
                    print(f'[Migration] chatbot_sessions.{col} skipped: {e}')
        m.commit()


    # ── Migration: user_tickets.assigned_to 座席字段 (2026-07-12) ──
    with get_db() as m:
        cols_t = get_table_columns(m, 'user_tickets')
        if 'assigned_to' not in cols_t:
            try:
                # DEFAULT 0 + FK→users(id) 是天生违约（0 非合法用户 id），
                # 任何不显式传该列的 INSERT 必 500。用 NULL（列可空，FK 放行）。
                m.execute("ALTER TABLE user_tickets ADD COLUMN assigned_to BIGINT DEFAULT NULL REFERENCES users(id)")
                print('[Migration] user_tickets.assigned_to added')
            except Exception as e:
                print(f'[Migration] user_tickets.assigned_to skipped: {e}')
        else:
            try:
                # 幂等纠正存量错误默认值（历史版本 DEFAULT 0 与 FK 冲突）
                bad = m.execute(
                    "SELECT pg_get_expr(d.adbin, d.adrelid) AS def "
                    "FROM pg_attribute a "
                    "JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                    "WHERE a.attrelid='user_tickets'::regclass "
                    "AND a.attname='assigned_to' "
                    "AND pg_get_expr(d.adbin, d.adrelid)='0'").fetchone()
                if bad:
                    m.execute('ALTER TABLE user_tickets ALTER COLUMN assigned_to SET DEFAULT NULL')
                    print('[Migration] user_tickets.assigned_to default fixed 0->NULL')
            except Exception as e:
                print(f'[Migration] assigned_to default fix skipped: {e}')
        if 'assigned_name' not in cols_t:
            try:
                m.execute("ALTER TABLE user_tickets ADD COLUMN assigned_name TEXT DEFAULT ''")
                print('[Migration] user_tickets.assigned_name added')
            except Exception as e:
                print(f'[Migration] user_tickets.assigned_name skipped: {e}')
        m.commit()

    # ── Migration: knowledge_blocks 智能记忆字段 + knowledge_history 表 (2026-07-18) ──
    with get_db() as m:
        kb_cols = get_table_columns(m, 'knowledge_blocks')
        for col_name, col_def in [
            ('source',        "VARCHAR(20) DEFAULT 'manual'"),
            ('hit_count',     "INTEGER DEFAULT 0"),
            ('quality_score', "REAL DEFAULT 0.5"),
            ('updated_at',    "TIMESTAMP DEFAULT NULL"),
            ('deleted_at',    "TIMESTAMP DEFAULT NULL"),
        ]:
            if col_name not in kb_cols:
                try:
                    m.execute(f"ALTER TABLE knowledge_blocks ADD COLUMN {col_name} {col_def}")
                    print(f'[Migration] knowledge_blocks.{col_name} added')
                except Exception as e:
                    m.rollback()  # clear aborted transaction
                    print(f'[Migration] knowledge_blocks.{col_name} skipped: {e}')
        m.commit()

        # 索引
        for idx_name, idx_col in [
            ('idx_kb_source',    'source'),
            ('idx_kb_quality',   'quality_score'),
            ('idx_kb_deleted',   'deleted_at'),
            ('idx_kb_hit_count', 'hit_count'),
        ]:
            try:
                m.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON knowledge_blocks({idx_col})")
            except Exception as e:
                print(f'[Migration] {idx_name} skipped: {e}')
        m.commit()

        # knowledge_history 版本历史表
        m.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_history (
                id               VARCHAR(64) PRIMARY KEY,
                kb_id            VARCHAR(64) NOT NULL,
                previous_title   VARCHAR(200),
                previous_content TEXT,
                changed_at       TIMESTAMP DEFAULT NOW()
            )
        """)
        m.execute('CREATE INDEX IF NOT EXISTS idx_kh_kb_id ON knowledge_history(kb_id)')
        m.commit()
        print('[Migration] knowledge_history table created')

    # ── Migration: knowledge_blocks 向量化（RAG 混合检索 — 2026-08-12）──
    # 向量路：pgvector 余弦检索。列不固定维度（生产 embedding 模型维度可能不同，
    # 种子默认 text-embedding-004 = 768）。当前知识库数据量小，顺序扫描足够；
    # 后续量大时再固定维度并建 HNSW 索引（见下方注释 SQL）。
    # pgvector 扩展缺失时优雅降级：不建向量列，检索自动走关键词路。
    with _safe_get_db_for_migration() as m:
        try:
            m.execute('CREATE EXTENSION IF NOT EXISTS vector')
            kb_cols = get_table_columns(m, 'knowledge_blocks')
            if 'embedding' not in kb_cols:
                m.execute('ALTER TABLE knowledge_blocks ADD COLUMN embedding vector')
                print('[Migration] knowledge_blocks.embedding added')
            # 升级到固定维度 + HNSW 索引（数据量大后执行，维度须与 embedding 模型一致）：
            # ALTER TABLE knowledge_blocks ALTER COLUMN embedding TYPE vector(768);
            # CREATE INDEX idx_kb_embedding ON knowledge_blocks USING hnsw (embedding vector_cosine_ops);
            m.commit()
            print('[Migration] knowledge_blocks pgvector column ready')
        except Exception as e:
            m.rollback()
            print(f'[Migration] knowledge_blocks embedding skipped (pgvector unavailable): {e}')

        # 关键词路：trigram 全文索引（PG16 内置 pg_trgm，中文 3-gram 可用）。
        try:
            m.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
            m.execute("CREATE INDEX IF NOT EXISTS idx_kb_content_trgm "
                      "ON knowledge_blocks USING gin (content gin_trgm_ops)")
            m.commit()
            print('[Migration] knowledge_blocks pg_trgm index created')
        except Exception as e:
            m.rollback()
            print(f'[Migration] knowledge_blocks pg_trgm index skipped: {e}')

    # ── Migration: knowledge_blocks 科研增强字段（科研版 — 2026-08-20）──
    # 学科/子学科分类、项目绑定、密级、元数据。project_id 为 UUID，
    # 逻辑关联 project_workspace.projects.id（不建硬 FK，避免与插件 schema 强耦合）。
    with get_db() as m:
        kb_cols = get_table_columns(m, 'knowledge_blocks')
        for col_name, col_def in [
            ('discipline',      "TEXT DEFAULT ''"),
            ('sub_discipline',  "TEXT DEFAULT ''"),
            ('project_id',      "UUID DEFAULT NULL"),
            ('confidentiality', "VARCHAR(20) NOT NULL DEFAULT 'internal'"),
            ('metadata',        "JSONB DEFAULT '{}'::jsonb"),
        ]:
            if col_name not in kb_cols:
                try:
                    m.execute(f"ALTER TABLE knowledge_blocks ADD COLUMN {col_name} {col_def}")
                    print(f'[Migration] knowledge_blocks.{col_name} added')
                except Exception as e:
                    m.rollback()
                    print(f'[Migration] knowledge_blocks.{col_name} skipped: {e}')
        m.commit()

        for idx_name, idx_col in [
            ('idx_kb_discipline',      'discipline'),
            ('idx_kb_sub_discipline',  'sub_discipline'),
            ('idx_kb_project',         'project_id'),
            ('idx_kb_confidentiality', 'confidentiality'),
        ]:
            try:
                m.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON knowledge_blocks({idx_col})")
            except Exception as e:
                print(f'[Migration] {idx_name} skipped: {e}')
        try:
            m.execute('CREATE INDEX IF NOT EXISTS idx_kb_meta ON knowledge_blocks USING gin(metadata)')
        except Exception as e:
            print(f'[Migration] idx_kb_meta skipped: {e}')
        m.commit()

    # ── Migration: system_kb_version 系统知识库版本追踪 (2026-07-24) ──
    with get_db() as m:
        m.execute('''CREATE TABLE IF NOT EXISTS system_kb_version (
            id              SERIAL PRIMARY KEY,
            version         VARCHAR(20) NOT NULL,
            release_date    TIMESTAMP DEFAULT NOW(),
            checksum        VARCHAR(64) NOT NULL,
            release_notes   TEXT DEFAULT '',
            update_url      TEXT DEFAULT '',
            applied         BOOLEAN DEFAULT FALSE,
            applied_at      TIMESTAMP DEFAULT NULL,
            applied_by      BIGINT DEFAULT NULL,
            created_at      TIMESTAMP DEFAULT NOW()
        )''')
        m.execute('CREATE INDEX IF NOT EXISTS idx_skv_version ON system_kb_version(version)')
        m.commit()
        print('[Migration] system_kb_version table created')

    # ── Migration: knowledge_queue 幂等 hash (2026-07-18) ──
    with get_db() as m:
        kq_cols = get_table_columns(m, 'knowledge_queue')
        if 'processed_hash' not in kq_cols:
            try:
                m.execute("ALTER TABLE knowledge_queue ADD COLUMN processed_hash VARCHAR(64) DEFAULT NULL")
                m.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_queue_hash ON knowledge_queue(processed_hash)")
                print('[Migration] knowledge_queue.processed_hash added')
            except Exception as e:
                print(f'[Migration] knowledge_queue.processed_hash skipped: {e}')
        m.commit()

    # ── Migration: ai_model_health + ai_model_failover_events（兜底引擎模型健康状态 2026-08-27）──
    with get_db() as m:
        m.execute('''CREATE TABLE IF NOT EXISTS ai_model_health (
            id                    SERIAL PRIMARY KEY,
            model_id              BIGINT NOT NULL DEFAULT 0,
            provider_slug         VARCHAR(64) NOT NULL DEFAULT '',
            model_name            VARCHAR(255) NOT NULL DEFAULT '',
            status                VARCHAR(20) NOT NULL DEFAULT 'unknown',
            consecutive_failures  BIGINT NOT NULL DEFAULT 0,
            circuit_open          BOOLEAN NOT NULL DEFAULT FALSE,
            total_calls           BIGINT NOT NULL DEFAULT 0,
            total_failures        BIGINT NOT NULL DEFAULT 0,
            last_success_at       TIMESTAMP DEFAULT NULL,
            last_failure_at       TIMESTAMP DEFAULT NULL,
            last_error            TEXT DEFAULT '',
            cooldown_until        TIMESTAMP DEFAULT NULL,
            created_at            TIMESTAMP DEFAULT NOW(),
            updated_at            TIMESTAMP DEFAULT NOW(),
            UNIQUE(provider_slug, model_name)
        )''')
        m.execute('CREATE INDEX IF NOT EXISTS idx_amh_model ON ai_model_health(model_id)')
        m.execute('''CREATE TABLE IF NOT EXISTS ai_model_failover_events (
            id            SERIAL PRIMARY KEY,
            from_model_id BIGINT NOT NULL DEFAULT 0,
            from_provider VARCHAR(64) NOT NULL DEFAULT '',
            from_model    VARCHAR(255) NOT NULL DEFAULT '',
            to_model_id   BIGINT NOT NULL DEFAULT 0,
            to_provider   VARCHAR(64) NOT NULL DEFAULT '',
            to_model      VARCHAR(255) NOT NULL DEFAULT '',
            reason        VARCHAR(64) NOT NULL DEFAULT 'health',
            error_text    TEXT DEFAULT '',
            request_id    VARCHAR(64) DEFAULT '',
            created_at    TIMESTAMP DEFAULT NOW()
        )''')
        m.execute('CREATE INDEX IF NOT EXISTS idx_amfe_created ON ai_model_failover_events(created_at)')
        m.commit()
        print('[Migration] ai_model_health + ai_model_failover_events created')


def _get_default_interests():
    """6 大类 ~35 个兴趣标签"""
    cats = [
        ("娱乐与媒体", ["电影","电视剧","音乐","动漫","游戏","阅读","短视频","综艺","纪录片"]),
        ("运动健身",   ["足球","篮球","跑步","健身","瑜伽","户外","游泳","滑雪","骑行"]),
        ("生活休闲",   ["旅行","美食","摄影","宠物","时尚","美妆","咖啡","露营","穿搭"]),
        ("科技知识",   ["IT","数码","编程","AI","财经","科学","历史","天文","区块链"]),
        ("艺术创作",   ["绘画","写作","手工艺","摄影","设计","书法","插画"]),
        ("其他",       ["健康养生","教育","环保","汽车","星座","心理学","投资","创业"]),
    ]
    tags = []
    for cat, items in cats:
        for i, n in enumerate(items):
            tags.append((n, cat, i+1, 1))  # is_hot=1
    return tags


# ── 模块级迁移守卫：全新库（核心表不存在）时，引用 init_db() 建表的迁移自动跳过 ──
def _table_exists(m, table: str) -> bool:
    """Return True if a table exists in the current search path."""
    row = m.execute(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema=ANY(current_schemas(false)) AND table_name=%s",
        (table,),
    ).fetchone()
    return bool(row['c'])


# ── Module-level: media_files table（防 init_db() 中途失败跳过）──
with _safe_get_db_for_migration() as m:
    m.execute('''CREATE TABLE IF NOT EXISTS media_files (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        filename        TEXT NOT NULL,
        original_name   TEXT NOT NULL,
        mime_type       TEXT NOT NULL DEFAULT 'application/octet-stream',
        file_size       BIGINT DEFAULT 0,
        file_path       TEXT NOT NULL,
        thumb_path      TEXT DEFAULT '',
        push_status     TEXT DEFAULT 'none',
        push_target     TEXT DEFAULT '',
        pushed_at       TEXT DEFAULT NULL,
        created_at      TIMESTAMP DEFAULT NOW(),
        updated_at      TIMESTAMP DEFAULT NOW()
    )''')
    m.execute('CREATE INDEX IF NOT EXISTS idx_mf_push_status ON media_files(push_status)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_mf_created ON media_files(created_at)')
    m.commit()
    print('[Migration] media_files table created (module-level)')

# ── Module-level: article_comments table（防 init_db() 中途失败跳过）──
with _safe_get_db_for_migration() as m:
    m.execute("""
        CREATE TABLE IF NOT EXISTS article_comments (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            post_id         BIGINT NOT NULL,
            parent_id       BIGINT,
            nickname        TEXT NOT NULL DEFAULT 'Anonymous',
            content         TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected')),
            ai_review       TEXT DEFAULT '',
            ai_score        BIGINT DEFAULT 0,
            ip_address      TEXT DEFAULT '',
            reviewed_by     BIGINT,
            reviewed_at     TIMESTAMP,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    m.execute("CREATE INDEX IF NOT EXISTS idx_article_comments_post ON article_comments(post_id)")
    m.execute("CREATE INDEX IF NOT EXISTS idx_article_comments_status ON article_comments(status)")
    m.commit()
    print('[Migration] article_comments table created (module-level)')


def get_active_model(provider_slug='deepseek'):
    """从 AI Hub (provider_models) 查询指定 provider 的第一个活跃模型。
    返回 (provider_model_id, model_name, base_url) 或 (None, None, None)。
    整个系统必须通过此函数获取模型，严禁硬编码模型名。"""
    try:
        with get_db() as conn:
            row = conn.execute("""
                SELECT pm.id, pm.model_name, pm.endpoint_url
                FROM provider_models pm
                JOIN providers p ON p.id = pm.provider_id
                WHERE p.slug = %s AND pm.is_active = 1 AND p.is_active = 1
                ORDER BY pm.id LIMIT 1
            """, (provider_slug,)).fetchone()
        if row:
            return row['id'], row['model_name'], row['endpoint_url'] or ''
        return None, None, None
    except Exception:
        return None, None, None

# ── 国际化: 市场特定表结构 (2026-06-29) ──
if MARKET == 'intl':
    with _safe_get_db_for_migration() as m:
        # INTL 用户表补充 OAuth 字段（CN 已有的 wechat/douyin 字段在 INTL 中保持空值）
        intl_cols = get_table_columns(m, 'users')
        intl_additions = {
            'country_code': "country_code TEXT DEFAULT ''",
            'google_id': "google_id TEXT",
            'github_id': "github_id TEXT",
            'facebook_id': "facebook_id TEXT",
        }
        for col_name, col_def in intl_additions.items():
            if col_name not in intl_cols:
                try:
                    m.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                    print(f'[i18n] users.{col_name} added')
                except Exception as e:
                    print(f'[i18n] users.{col_name} skipped: {e}')

        # INTL 地址表（自由文本）
        m.execute('''CREATE TABLE IF NOT EXISTS user_addresses_intl (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id         BIGINT NOT NULL,
            label           TEXT DEFAULT '',
            recipient_name  TEXT NOT NULL DEFAULT '',
            phone           TEXT NOT NULL DEFAULT '',
            country         TEXT NOT NULL DEFAULT '',
            state           TEXT DEFAULT '',
            city            TEXT DEFAULT '',
            address_line1   TEXT NOT NULL DEFAULT '',
            address_line2   TEXT DEFAULT '',
            postal_code     TEXT DEFAULT '',
            is_default      BIGINT DEFAULT 0,
            status          BIGINT DEFAULT 1,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )''')
        m.execute('CREATE INDEX IF NOT EXISTS idx_addr_intl_user ON user_addresses_intl(user_id)')

        print('[i18n] ✅ INTL-specific tables and data initialized')
# ── 客户管理: 企业认证字段 + 审核表 (CN/INTL通用) ──
with _safe_get_db_for_migration() as m:
    if not _table_exists(m, 'users'):
        print('[Migration] users table not created yet, skip enterprise/oauth fields')
    else:
        user_cols = get_table_columns(m, 'users')
        enterprise_fields = {
            'enterprise_name': "enterprise_name TEXT DEFAULT ''",
            'enterprise_tax_id': "enterprise_tax_id TEXT DEFAULT ''",
            'enterprise_address': "enterprise_address TEXT DEFAULT ''",
            'enterprise_phone': "enterprise_phone TEXT DEFAULT ''",
            'enterprise_bank': "enterprise_bank TEXT DEFAULT ''",
            'enterprise_bank_acct': "enterprise_bank_acct TEXT DEFAULT ''",
            'enterprise_verified': "enterprise_verified BIGINT DEFAULT 0",
            'enterprise_verified_at': "enterprise_verified_at TEXT",
        }
        for col_name, col_def in enterprise_fields.items():
            if col_name not in user_cols:
                try:
                    m.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                except Exception as e:
                    print(f'[migration] users.{col_name} skipped: {e}')

        # ── Migration: alipay_user_id + telegram_open_id (2026-07-11) ──
        oauth_user_fields = {
            'alipay_user_id': "alipay_user_id TEXT UNIQUE",
            'telegram_open_id': "telegram_open_id TEXT UNIQUE",
        }
        for col_name, col_def in oauth_user_fields.items():
            if col_name not in user_cols:
                try:
                    m.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                except Exception as e:
                    print(f'[migration] users.{col_name} skipped: {e}')


# ── i18n 翻译表 (2026-06-30) ──
with _safe_get_db_for_migration() as m:
    m.execute('''CREATE TABLE IF NOT EXISTS i18n_strings (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        locale      TEXT NOT NULL DEFAULT 'zh-CN',
        source_hash TEXT NOT NULL,
        source      TEXT NOT NULL,
        translation TEXT NOT NULL DEFAULT '',
        is_auto     BIGINT DEFAULT 0,
        updated_at  TIMESTAMP DEFAULT NOW(),
        created_at  TIMESTAMP DEFAULT NOW(),
        UNIQUE(locale, source_hash)
    )''')
    m.execute('CREATE INDEX IF NOT EXISTS idx_i18n_locale ON i18n_strings(locale)')
    print('[i18n] ✅ i18n_strings table created')

# ── Migration: site_domains 子域名管理表 (2026-07-06) ──
# Note: 移除了 FOREIGN KEY 引用 site_configs，因为 site_configs 在 init_db() 中创建
# 模块级 migration 执行时 site_configs 可能尚未建表
with _safe_get_db_for_migration() as m:
    m.execute('''CREATE TABLE IF NOT EXISTS site_domains (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        site_config_id  BIGINT NOT NULL DEFAULT 1,
        subdomain       TEXT NOT NULL,
        full_domain     TEXT NOT NULL UNIQUE,
        display_name    TEXT NOT NULL DEFAULT '',
        template        TEXT DEFAULT 'default',
        is_published    BIGINT DEFAULT 1,
        page_keys_json  TEXT DEFAULT '["home"]',
        sort_order      BIGINT DEFAULT 0,
        service_port    BIGINT DEFAULT NULL,
        created_at      TIMESTAMP DEFAULT NOW(),
        updated_at      TIMESTAMP DEFAULT NOW()
    )''')
    m.execute('CREATE INDEX IF NOT EXISTS idx_sd_config ON site_domains(site_config_id)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_sd_domain ON site_domains(full_domain)')
    m.commit()
    print('[Migration] site_domains (subdomain management) table created')

# ── Migration: site_domains 新增 service_port 列 (2026-07-06) ──
try:
    with _safe_get_db_for_migration() as m:
        m.execute("ALTER TABLE site_domains ADD COLUMN service_port BIGINT DEFAULT NULL")
        m.commit()
        print('[Migration] site_domains.service_port column added')
except Exception:
    pass  # 列已存在

# 默认主页站点在 site_configs 中创建（如不存在）
_default_domain = os.environ.get('DEPLOY_DOMAIN', 'localhost')
_default_brand = os.environ.get('DEPLOY_BRAND', 'VeroRun 维洛智能')
try:
    with _safe_get_db_for_migration() as m:
        m.execute(
            "INSERT INTO site_configs (id, domain, name, industry, tier, features) OVERRIDING SYSTEM VALUE VALUES (1, %s, %s, 'ai', 'self_hosted', '[\"main\"]') ON CONFLICT (id) DO NOTHING",
            (_default_domain, _default_brand)
        )
        m.commit()
except Exception:
    pass  # site_configs 表可能尚未创建

# site_domains 默认种子（3 个标准子域名）
_defaults = [
    ('www',      f'www.{_default_domain}',      f'{_default_brand} 官网',       'default', 1, 1),
    ('agent',    f'agent.{_default_domain}',    f'{_default_brand} 管理后台',   'default', 1, 2),
    ('platform', f'platform.{_default_domain}', f'{_default_brand} 用户中心',   'default', 1, 3),
]
try:
    with _safe_get_db_for_migration() as m:
        for sub, full, name, template, pub, so in _defaults:
            m.execute(
                "INSERT INTO site_domains (site_config_id, subdomain, full_domain, display_name, template, is_published, sort_order) VALUES (1, %s, %s, %s, %s, %s, %s) ON CONFLICT (full_domain) DO NOTHING",
                (sub, full, name, template, pub, so)
            )
        m.commit()
    print('[Migration] site_domains default seeds (www/agent/platform)')
except Exception:
    pass  # site_domains 表可能尚未创建


# ── Site Builder 模块表 (2026-07-11) ──
with _safe_get_db_for_migration() as m:
    m.execute('''CREATE TABLE IF NOT EXISTS site_builder_prompts (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        identifier      TEXT UNIQUE NOT NULL,
        name            TEXT NOT NULL,
        description     TEXT DEFAULT '',
        icon            TEXT DEFAULT '📄',
        industry        TEXT DEFAULT '',
        tags_json       TEXT DEFAULT '[]',
        is_builtin      BIGINT DEFAULT 1,
        is_active       BIGINT DEFAULT 1,
        defaults_json   TEXT DEFAULT '{}',
        pages_json      TEXT DEFAULT '[]',
        documents_json  TEXT DEFAULT '[]',
        prompts_json    TEXT DEFAULT '{}',
        created_by      BIGINT DEFAULT 0,
        created_at      TIMESTAMP DEFAULT NOW(),
        updated_at      TIMESTAMP DEFAULT NOW()
    )''')
    m.execute('''CREATE TABLE IF NOT EXISTS site_builder_tasks (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        task_id         TEXT UNIQUE NOT NULL,
        user_id         BIGINT NOT NULL,
        site_config_id  BIGINT DEFAULT 1,
        prompt_id       INTEGER,
        user_input      TEXT DEFAULT '',
        status          TEXT DEFAULT 'pending',
        plan_json       TEXT DEFAULT '{}',
        result_json     TEXT DEFAULT '{}',
        current_step    TEXT DEFAULT '',
        error_message   TEXT DEFAULT '',
        created_at      TIMESTAMP DEFAULT NOW(),
        updated_at      TIMESTAMP DEFAULT NOW(),
        finished_at     TEXT DEFAULT ''
    )''')
    m.execute('CREATE INDEX IF NOT EXISTS idx_sbp_identifier ON site_builder_prompts(identifier)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_sbp_industry ON site_builder_prompts(industry)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_sbt_user ON site_builder_tasks(user_id)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_sbt_status ON site_builder_tasks(status)')
    m.commit()
    print('[Migration] site_builder table created')

    # ── site_settings: 统一设计令牌表（替代 brand_settings + header_nav + footer_* + themes）──
    try:
        m.execute("""
            CREATE TABLE IF NOT EXISTS design_tokens (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site_key        TEXT NOT NULL DEFAULT 'platform',
                token_json      TEXT DEFAULT '{}',
                generated_by    TEXT DEFAULT 'manual',
                prompt_id       BIGINT DEFAULT NULL,
                version         BIGINT DEFAULT 1,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE(site_key)
            );
        """)
        m.execute("CREATE INDEX IF NOT EXISTS idx_dt_site_key ON design_tokens(site_key)")
        m.commit()
        print('[Migration] design_tokens table created')
    except Exception as e_th:
        print(f'[Migration] design_tokens table creation failed (may already exist): {e_th}')


def now_iso():
    return datetime.now().isoformat()


TIERS = {
    'free':     {'name': 'Free',     'daily_limit': 20,   'price_month': 0,   'price_year': 0,    'features': ['basic'],       'desc': '每日20次调用', 'max_agents': 1},
    'standard': {'name': 'Standard', 'daily_limit': 100,  'price_month': 88,  'price_year': 888,  'features': ['basic', 'sentiment', 'market'], 'desc': '每日100次调用', 'max_agents': 3},
    'pro':      {'name': 'Pro',      'daily_limit': 1000, 'price_month': 188, 'price_year': 1888,  'features': ['all'],         'desc': '每日1000次调用', 'max_agents': 10},
}

# ── Migration: provider_api_keys 统一 LLM 供应商 Key 管理 ──
# 注意：此表与用户 API Key 表 (api_keys) 不同，专用于管理 LLM 供应商的 API Key
with _safe_get_db_for_migration() as m:
    m.execute('''
        CREATE TABLE IF NOT EXISTS provider_api_keys (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name            TEXT NOT NULL DEFAULT '',
            key_value_enc   TEXT NOT NULL DEFAULT '',
            provider        TEXT NOT NULL DEFAULT '',
            description     TEXT DEFAULT '',
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(name, provider)
        )
    ''')

    seed_keys = [
        ('Alibaba Cloud DashScope', '', 'dashscope', 'Qwen series models API Key'),
        ('OpenAI',                  '', 'openai',    'GPT-4o series models API Key'),
        ('DeepSeek',                '', 'deepseek',  'DeepSeek models API Key'),
        ('OpenRouter',              '', 'openrouter','Multi-model aggregation API Key'),
        ('SiliconFlow',             '', 'siliconflow','SiliconFlow platform API Key'),
        ('Google Gemini',            '', 'gemini',    'Google Gemini API Key'),
        ('xAI Grok',                 '', 'grok',      'xAI Grok API Key'),
        ('KIMI',                     '', 'kimi',      'Moonshot AI / 月之暗面 API Key'),
        ('Zhipu',                    '', 'zhipu',     '智谱 AI / ChatGLM API Key'),
    ]
    for name, key_val, provider, desc in seed_keys:
        m.execute(
            "INSERT INTO provider_api_keys (name, key_value_enc, provider, description) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (name, provider) DO NOTHING",
            (name, key_val, provider, desc)
        )
    m.commit()
    print('[Migration] provider_api_keys table + seed data created')

with _safe_get_db_for_migration() as m:
    try:
        pm_cols = get_table_columns(m, 'provider_models')
        if 'api_key_id' not in pm_cols:
            m.execute('ALTER TABLE provider_models ADD COLUMN api_key_id BIGINT DEFAULT NULL REFERENCES provider_api_keys(id)')
            print('[Migration] provider_models.api_key_id added')
        m.commit()
    except Exception:
        m.rollback()

# ── Migration: provider_models.embedding_dim（embedding 向量维度）──
with _safe_get_db_for_migration() as m:
    try:
        pm_cols = get_table_columns(m, 'provider_models')
        if 'embedding_dim' not in pm_cols:
            m.execute('ALTER TABLE provider_models ADD COLUMN embedding_dim INTEGER NOT NULL DEFAULT 1536')
            print('[Migration] provider_models.embedding_dim added')
        # Google text-embedding-004 实际输出 768 维，更新种子数据
        m.execute(
            "UPDATE provider_models SET embedding_dim = 768"
            " WHERE model_name = 'text-embedding-004'"
            " AND capabilities LIKE '%embedding%'"
            " AND embedding_dim <> 768"
        )
        m.commit()
    except Exception:
        m.rollback()

# ── Migration: llm_quotas 精细化配额管理 ──
with _safe_get_db_for_migration() as m:
    m.execute('''
        CREATE TABLE IF NOT EXISTS llm_quotas (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            target_type     TEXT NOT NULL CHECK(target_type IN ('user','model','module','global')),
            target_id       BIGINT DEFAULT NULL,
            daily_limit     BIGINT DEFAULT 0,
            rate_limit      BIGINT DEFAULT 0,
            rate_window_sec BIGINT DEFAULT 60,
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    ''')
    # Clean up duplicate rows before creating unique index
    m.execute('''
        DELETE FROM llm_quotas a USING llm_quotas b
        WHERE a.id > b.id
        AND a.target_type = b.target_type
        AND COALESCE(a.target_id, -1) = COALESCE(b.target_id, -1)
    ''')
    m.commit()
    m.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_quotas_type
        ON llm_quotas (target_type, COALESCE(target_id, -1))
    ''')
    # Add target_key column (non-destructive migration)
    try:
        m.execute("ALTER TABLE llm_quotas ADD COLUMN target_key VARCHAR(100) DEFAULT NULL")
    except Exception:
        m._conn.rollback()  # rollback aborted transaction
        pass  # column already exists

    # 种子数据：全站默认配额
    m.execute(
        "INSERT INTO llm_quotas (target_type, daily_limit, rate_limit) "
        "VALUES ('global', 2000000, 30) "
        "ON CONFLICT (target_type, COALESCE(target_id, -1)) DO NOTHING"
    )
    m.commit()
    print('[Migration] llm_quotas table + default seed created')


# ── Migration: unified_api_keys — Phase 3 unified API key management ──
with _safe_get_db_for_migration() as m:
    m.execute('''
        CREATE TABLE IF NOT EXISTS unified_api_keys (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key_hash        TEXT UNIQUE NOT NULL,
            key_prefix      TEXT NOT NULL,
            key_type        TEXT NOT NULL CHECK(key_type IN ('user','agent','provider')),
            user_id         BIGINT NOT NULL,
            agent_id        BIGINT DEFAULT NULL,
            provider        TEXT DEFAULT '',
            name            TEXT DEFAULT '',
            scopes          TEXT DEFAULT '[]',
            quota_daily     BIGINT DEFAULT NULL,
            expire_at       TIMESTAMP DEFAULT NULL,
            status          TEXT DEFAULT 'active' CHECK(status IN ('active','revoked','expired')),
            calls_total     BIGINT DEFAULT 0,
            calls_today     BIGINT DEFAULT 0,
            last_used_at    TIMESTAMP DEFAULT NULL,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    ''')
    m.execute('''
        CREATE TABLE IF NOT EXISTS api_key_audit (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key_id          BIGINT NOT NULL REFERENCES unified_api_keys(id),
            action          TEXT NOT NULL CHECK(action IN ('create','revoke','rotate')),
            user_id         BIGINT DEFAULT NULL,
            extra           TEXT DEFAULT '{}',
            created_at      TIMESTAMP DEFAULT NOW()
        )
    ''')
    m.execute('''
        CREATE TABLE IF NOT EXISTS usage_quotas (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            target_type     TEXT NOT NULL CHECK(target_type IN ('key','user','agent','global')),
            target_id       BIGINT DEFAULT NULL,
            daily_limit     BIGINT DEFAULT 1000,
            rate_limit      BIGINT DEFAULT 10,
            rate_window_sec BIGINT DEFAULT 60,
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    ''')
    m.commit()
    # Create indexes
    m.execute('CREATE INDEX IF NOT EXISTS idx_unified_keys_user ON unified_api_keys(user_id)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_unified_keys_type ON unified_api_keys(key_type)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_unified_keys_hash ON unified_api_keys(key_hash)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_unified_keys_status ON unified_api_keys(status)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_apikey_audit_key ON api_key_audit(key_id)')
    m.commit()

    # Seed default global quota
    m.execute(
        "INSERT INTO usage_quotas (target_type, daily_limit, rate_limit) "
        "VALUES ('global', 100000, 30) "
        "ON CONFLICT DO NOTHING"
    )
    m.commit()
    print('[Migration] unified_api_keys + api_key_audit + usage_quotas tables created')

# ── Migration: unified subscription — Phase 4 base plan + plugin addons ──
with _safe_get_db_for_migration() as m:
    m.execute('''
        CREATE TABLE IF NOT EXISTS base_plans (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            plan_key        TEXT UNIQUE NOT NULL,
            name            TEXT NOT NULL,
            description     TEXT DEFAULT '',
            daily_limit     BIGINT DEFAULT 20,
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    ''')
    m.execute('''
        CREATE TABLE IF NOT EXISTS plugin_products (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            plugin_key      TEXT UNIQUE NOT NULL,
            name            TEXT NOT NULL,
            description     TEXT DEFAULT '',
            icon            TEXT DEFAULT '',
            category        TEXT DEFAULT '',
            price_month_fen BIGINT DEFAULT 0,
            price_year_fen  BIGINT DEFAULT 0,
            sort_order      BIGINT DEFAULT 0,
            is_featured     BIGINT DEFAULT 0,
            is_active       BIGINT DEFAULT 1,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    ''')
    m.execute('''
        CREATE TABLE IF NOT EXISTS user_subscriptions (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id         BIGINT NOT NULL,
            plan_key        TEXT DEFAULT 'free',
            status          TEXT DEFAULT 'active' CHECK(status IN ('active','cancelled','expired')),
            daily_limit     BIGINT DEFAULT 20,
            calls_today     BIGINT DEFAULT 0,
            calls_total     BIGINT DEFAULT 0,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id)
        )
    ''')
    m.execute('''
        CREATE TABLE IF NOT EXISTS subscription_addons (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id         BIGINT NOT NULL,
            plugin_key      TEXT NOT NULL,
            plugin_name     TEXT DEFAULT '',
            period          TEXT DEFAULT 'month',
            period_start    TIMESTAMP DEFAULT NOW(),
            period_end      TIMESTAMP DEFAULT NULL,
            price_fen       BIGINT DEFAULT 0,
            payment_method  TEXT DEFAULT 'wechat',
            status          TEXT DEFAULT 'active' CHECK(status IN ('active','cancelled','expired')),
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, plugin_key)
        )
    ''')
    m.commit()
    # Create indexes
    m.execute('CREATE INDEX IF NOT EXISTS idx_base_plans_key ON base_plans(plan_key)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_plugin_products_key ON plugin_products(plugin_key)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_plugin_products_cat ON plugin_products(category)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_user_subs_user ON user_subscriptions(user_id)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_addons_user ON subscription_addons(user_id)')
    m.execute('CREATE INDEX IF NOT EXISTS idx_addons_plugin ON subscription_addons(plugin_key)')
    m.commit()

    # Seed default free plan
    m.execute(
        "INSERT INTO base_plans (plan_key, name, description, daily_limit) "
        "VALUES ('free', 'Free', 'Free entry plan with 20 API calls per day', 20) "
        "ON CONFLICT (plan_key) DO NOTHING"
    )
    m.commit()
    print('[Migration] base_plans + plugin_products + user_subscriptions + subscription_addons created')

if __name__ == "__main__":
    init_db()
    print(f"OK: {DB_PATH}")
