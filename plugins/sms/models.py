#!/usr/bin/env python3
"""
SMS Plugin Models — 独立 PG schema `sms`
=====================================
完全独立于主库，不依赖主系统 models。
- sms_templates: 短信模板（从主库迁移）
- sms_logs: 短信发送日志
"""
from i18n import _
import psycopg2
from plugins._base.db import PgConnection
from plugins._base.db import get_raw_connection
from plugins._base.db import get_pooled_connection


def get_sms_db():
    """每次从共享池借取连接并设置 sms schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS sms")
        conn.execute("SET search_path TO sms")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[SmsPlugin] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS sms")
        conn.execute("SET search_path TO sms")
        return conn


def init_sms_db():
    """初始化短信插件数据库表（幂等）"""
    with get_sms_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS sms_templates (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            category        TEXT NOT NULL,
            name            TEXT NOT NULL,
            template_code   TEXT NOT NULL,
            note            TEXT DEFAULT '',
            sort_order      BIGINT DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(category, name)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS sms_logs (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            phone           TEXT NOT NULL,
            code            TEXT DEFAULT '',
            purpose         TEXT DEFAULT '',
            provider        TEXT DEFAULT '',
            status          TEXT DEFAULT 'sent',
            error           TEXT DEFAULT '',
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sms_logs_phone ON sms_logs(phone)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sms_logs_created ON sms_logs(created_at)')
        conn.execute('''CREATE TABLE IF NOT EXISTS sms_rate_limits (
            phone       TEXT NOT NULL,
            hour_bucket TEXT NOT NULL,
            count       BIGINT DEFAULT 1,
            PRIMARY KEY (phone, hour_bucket)
        )''')
        _ensure_time_columns(conn)
        conn.commit()
        print(_('[SmsPlugin] sms schema has been initialized'))


def _ensure_time_columns(conn):
    """升级历史 TEXT 时间列为 TIMESTAMPTZ（幂等，兼容已存在的旧表）。

    CREATE TABLE IF NOT EXISTS 不会修改已存在的表，
    此处通过 ALTER 对存量表做一次性类型升级。
    """
    for table, col in (('sms_templates', 'created_at'),
                       ('sms_templates', 'updated_at'),
                       ('sms_logs', 'created_at')):
        try:
            conn.execute(f'ALTER TABLE {table} ALTER COLUMN {col} TYPE TIMESTAMPTZ USING {col}::timestamptz')
            conn.commit()
        except Exception:
            conn.rollback()  # 表/列不存在或已是 TIMESTAMPTZ 时忽略


def migrate_from_main_db():
    """从主库幂等迁移 sms_templates 数据"""
    with get_sms_db() as conn:
        existing = conn.execute('SELECT COUNT(*) FROM sms_templates').fetchone()['count']
        if existing > 0:
            print(_('[SmsPlugin] sms_templates already has data, migration skipped'))
            return

        # 直接通过 _base.db 建立独立主库连接（显式 search_path=public），
        # 不再修改 sys.path / 依赖 auth-center 内部 models。
        main_conn = None
        try:
            raw = get_raw_connection()
            raw.autocommit = False
            main_conn = PgConnection(raw)
            main_conn.execute('SET search_path TO public')
            rows = main_conn.execute(
                'SELECT category, name, template_code, note, sort_order FROM sms_templates ORDER BY sort_order'
            ).fetchall()
        except Exception as e:
            print(f'[SmsPlugin] Failed to read sms_templates from main database (may not exist): {e}')
            return
        finally:
            if main_conn:
                main_conn.close()

        count = 0
        for r in rows:
            cur = conn.execute(
                'INSERT INTO sms_templates (category, name, template_code, note, sort_order) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (category, name) DO NOTHING',
                (r['category'], r['name'], r['template_code'], r['note'], r['sort_order'])
            )
            # 用 rowcount 统计实际插入条数（跳过 ON CONFLICT DO NOTHING 冲突行）
            if cur.rowcount and cur.rowcount > 0:
                count += 1
        conn.commit()
        print(f'[SmsPlugin] Migrated {count} sms_templates records from main database')


# 兼容旧接口名
ensure_sms_tables = init_sms_db
