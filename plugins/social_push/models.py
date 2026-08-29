#!/usr/bin/env python3
"""Social Push Plugin — 数据库模型

独立 PG schema `social_push`，存放社媒发布日志表 social_push_logs。
从主库迁移而来，表结构保持一致。
"""
import psycopg2
from plugins._base.db import get_pooled_connection


def get_sp_db():
    """每次从共享池借取连接并设置 social_push schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS social_push")
        conn.execute("SET search_path TO social_push")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[SocialPushPlugin] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS social_push")
        conn.execute("SET search_path TO social_push")
        return conn


def init_sp_db():
    """初始化社媒发布日志表（幂等）。

    表结构与主库 social_push_logs 保持一致，便于数据迁移。
    admin_id 原主库为 REFERENCES users(id) 外键；插件独立库不跨库外键，
    仅保留列（值仍是主库 users.id），符合_("Independent Database + Primary Database Read-Only")契约。
    """
    with get_sp_db() as conn:
        conn.execute("""
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
                push_time       TEXT,
                admin_id        BIGINT,
                error_msg       TEXT DEFAULT '',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # 社媒账号表（数据库表单化管理，凭证加密存储）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                platform      TEXT NOT NULL,
                account_name  TEXT NOT NULL DEFAULT '',
                app_id        TEXT DEFAULT '',
                app_secret    TEXT DEFAULT '',
                api_key       TEXT DEFAULT '',
                api_secret    TEXT DEFAULT '',
                access_token  TEXT DEFAULT '',
                access_secret TEXT DEFAULT '',
                bearer_token  TEXT DEFAULT '',
                client_id     TEXT DEFAULT '',
                client_secret TEXT DEFAULT '',
                username      TEXT DEFAULT '',
                password      TEXT DEFAULT '',
                bot_token     TEXT DEFAULT '',
                channel       TEXT DEFAULT '',
                token         TEXT DEFAULT '',
                extra_config  TEXT DEFAULT '{}',
                is_active     INTEGER DEFAULT 1,
                created_at    TIMESTAMPTZ DEFAULT NOW(),
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()

    # 幂等迁移：历史 TEXT 列一次性转 TIMESTAMPTZ；已是 TIMESTAMPTZ 时为无操作
    with get_sp_db() as conn:
        try:
            conn.execute(
                "ALTER TABLE social_push_logs ALTER COLUMN created_at TYPE TIMESTAMPTZ "
                "USING created_at::timestamptz"
            )
            conn.commit()
        except Exception:
            conn.rollback()


def migrate_from_main_db():
    """从主库 social_push_logs 迁移历史发布记录到插件库（幂等）。

    仅当插件库为空时导入（避免重复导入 / 覆盖新数据）。
    主库无该表时静默跳过。返回迁移的记录数。
    """
    try:
        from models import get_db as get_main_db
    except Exception:
        return 0

    with get_sp_db() as conn:
        # 插件库已有数据则跳过，保证幂等且不重复导入
        existing = conn.execute("SELECT COUNT(*) AS c FROM social_push_logs").fetchone()
        if existing and existing['c'] > 0:
            return 0

        try:
            with get_main_db() as main:
                has_table = main.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_name='social_push_logs'"
                ).fetchone()
                if not has_table:
                    return 0
                rows = main.execute(
                    "SELECT platform, content_type, title, summary, article_json, media_id, "
                    "publish_id, status, push_time, admin_id, error_msg, created_at "
                    "FROM social_push_logs ORDER BY id"
                ).fetchall()
        except Exception:
            return 0

        migrated = 0
        for r in rows:
            conn.execute(
                """INSERT INTO social_push_logs
                   (platform, content_type, title, summary, article_json, media_id,
                    publish_id, status, push_time, admin_id, error_msg, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (r['platform'], r['content_type'], r['title'], r['summary'],
                 r['article_json'], r['media_id'], r['publish_id'], r['status'],
                 r['push_time'], r['admin_id'], r['error_msg'], r['created_at'])
            )
            migrated += 1
        conn.commit()
    return migrated


# ════════════════════════════════════════════════════════════════
# 社媒账号表（数据库表单化管理）
# ════════════════════════════════════════════════════════════════

# 敏感字段（加密存储）
SENSITIVE_COLUMNS = ('app_secret', 'api_secret', 'access_token', 'access_secret',
                     'bearer_token', 'client_secret', 'password', 'bot_token', 'token')

# 平台 → 发布配置 key 映射（与 routes/providers 现有 system_config key 一致）
PLATFORM_CONFIG_MAP = {
    'wechat':   {'app_id': 'wechat_app_id', 'app_secret': 'wechat_app_secret', 'token': 'wechat_token'},
    'weibo':    {'api_key': 'weibo_app_key', 'app_secret': 'weibo_app_secret', 'access_token': 'weibo_access_token'},
    'toutiao':  {'app_id': 'toutiao_app_id', 'app_secret': 'toutiao_app_secret', 'access_token': 'toutiao_access_token'},
    'twitter':  {'api_key': 'twitter_api_key', 'api_secret': 'twitter_api_secret',
                 'access_token': 'twitter_access_token', 'access_secret': 'twitter_access_secret',
                 'bearer_token': 'twitter_bearer_token'},
    'linkedin': {'client_id': 'linkedin_client_id', 'client_secret': 'linkedin_client_secret',
                 'access_token': 'linkedin_access_token'},
    'reddit':   {'client_id': 'reddit_client_id', 'client_secret': 'reddit_client_secret',
                 'username': 'reddit_username', 'password': 'reddit_password'},
    'telegram': {'bot_token': 'telegram_bot_token', 'channel': 'telegram_channel'},
}

_ACCOUNT_COLUMNS = ('id', 'platform', 'account_name', 'app_id', 'app_secret', 'api_key',
                    'api_secret', 'access_token', 'access_secret', 'bearer_token',
                    'client_id', 'client_secret', 'username', 'password', 'bot_token',
                    'channel', 'token', 'extra_config', 'is_active', 'created_at', 'updated_at')


def _row_to_dict(row):
    return {c: row[c] for c in _ACCOUNT_COLUMNS if c in row.keys()}


def _decrypt_row(row):
    """解密全部敏感字段，返回完整明文账号行。"""
    from .crypto import decrypt
    d = _row_to_dict(row)
    for col in SENSITIVE_COLUMNS:
        if d.get(col):
            d[col] = decrypt(d[col])
    return d


def _mask_row(row):
    """返回脱敏账号行（敏感字段置空，另附 *_masked 显示值）。"""
    from .crypto import decrypt, mask
    d = _decrypt_row(row)
    for col in SENSITIVE_COLUMNS:
        if d.get(col):
            d[col + '_masked'] = mask(d[col])
            d[col] = ''
    return d


def list_accounts(platform=None, active_only=False):
    """账号列表（脱敏）。可按平台过滤 / 仅启用。"""
    sql = "SELECT * FROM accounts"
    conds, params = [], []
    if platform:
        conds.append("platform=%s")
        params.append(platform)
    if active_only:
        conds.append("is_active=1")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY platform, id"
    with get_sp_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_mask_row(r) for r in rows]


def get_account(account_id):
    """单个账号（脱敏）。"""
    with get_sp_db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()
    return _mask_row(row) if row else None


def get_account_raw(account_id):
    """单个账号（解密明文，供发布读取）。"""
    with get_sp_db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()
    return _decrypt_row(row) if row else None


def get_active_account_raw(platform):
    """指定平台当前启用的账号（解密明文，供发布读取）。"""
    with get_sp_db() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE platform=%s AND is_active=1 ORDER BY id LIMIT 1",
            (platform,)
        ).fetchone()
    return _decrypt_row(row) if row else None


def create_account(data):
    """新增账号。data 为字典（字段名与表列一致，敏感字段传入明文）。返回新 id。"""
    from .crypto import encrypt
    allowed = {c: data[c] for c in data
               if c in _ACCOUNT_COLUMNS and c not in ('id', 'created_at', 'updated_at')}
    for col in SENSITIVE_COLUMNS:
        if allowed.get(col):
            allowed[col] = encrypt(allowed[col])
    cols = list(allowed.keys())
    sql = f"INSERT INTO accounts ({','.join(cols)}) VALUES ({','.join('%s' for _ in cols)}) RETURNING id"
    with get_sp_db() as conn:
        row = conn.execute(sql, [allowed[c] for c in cols]).fetchone()
        conn.commit()
    return row['id']


def update_account(account_id, data):
    """更新账号。敏感字段传空串/None 表示不修改。返回是否发生更新。"""
    from .crypto import encrypt
    allowed = {c: data[c] for c in data
               if c in _ACCOUNT_COLUMNS and c not in ('id', 'created_at', 'updated_at')}
    sets, params = [], []
    for c, v in allowed.items():
        if c in SENSITIVE_COLUMNS:
            if v in ('', None):
                continue  # 空值 = 不修改
            v = encrypt(v)
        sets.append(f"{c}=%s")
        params.append(v)
    if not sets:
        return False
    sets.append('updated_at=NOW()')
    params.append(account_id)
    with get_sp_db() as conn:
        conn.execute(f"UPDATE accounts SET {','.join(sets)} WHERE id=%s", params)
        conn.commit()
    return True


def delete_account(account_id):
    """删除账号。"""
    with get_sp_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
        conn.commit()
    return True


def account_to_config(account):
    """把已解密的账号行转成发布配置 dict（key 与 routes/providers 现有 key 一致）。"""
    cfg = {}
    mapping = PLATFORM_CONFIG_MAP.get((account or {}).get('platform'))
    if not mapping:
        return cfg
    for col, key in mapping.items():
        val = account.get(col)
        if val:
            cfg[key] = val
    return cfg


def has_account_configured(platform):
    """平台是否已配置账号（含凭证），供 check_config 使用。"""
    row = get_active_account_raw(platform)
    return bool(row)
