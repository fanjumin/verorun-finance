"""
Coupons Plugin — PostgreSQL schema: coupons
============================================
插件自己的 coupons schema，主库只读 users/order_items/api_logs/admin_actions 表。
"""

import os
from contextlib import contextmanager
from plugins._base.db import PgConnection, get_raw_connection

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


@contextmanager
def get_db():
    """连接插件自己的数据库（PG schema: coupons）。"""
    raw = get_raw_connection()
    raw.autocommit = False
    raw.cursor().execute("CREATE SCHEMA IF NOT EXISTS coupons")
    raw.commit()
    raw.cursor().execute("SET search_path TO coupons")
    raw.commit()
    conn = PgConnection(raw)
    try:
        yield conn
    finally:
        raw.close()


def init_db():
    """创建插件自己的表。"""
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS coupons (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code            TEXT UNIQUE NOT NULL,
            name            TEXT DEFAULT '',
            coupon_type     TEXT DEFAULT 'fixed',
            value           DOUBLE PRECISION NOT NULL DEFAULT 0,
            min_amount      DOUBLE PRECISION DEFAULT 0,
            min_quantity    BIGINT DEFAULT 0,
            usage_limit     BIGINT DEFAULT 0,
            used_count      BIGINT DEFAULT 0,
            per_user_limit  BIGINT DEFAULT 1,
            expire_at       TIMESTAMPTZ,
            is_active       BIGINT DEFAULT 1,
            description     TEXT DEFAULT '',
            coupon_category TEXT DEFAULT 'general',
            applicable_products TEXT DEFAULT '',
            applicable_plans    TEXT DEFAULT '',
            scene           TEXT DEFAULT '',
            first_month_only   BIGINT DEFAULT 0,
            stackable       BIGINT DEFAULT 0,
            active_from     TIMESTAMPTZ,
            active_to       TIMESTAMPTZ,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS coupon_redemptions (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            coupon_id       BIGINT NOT NULL REFERENCES coupons(id),
            user_id         BIGINT NOT NULL,
            order_no        TEXT NOT NULL,
            discount_fen    BIGINT NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_meta (
            key         TEXT PRIMARY KEY,
            value       TEXT DEFAULT '',
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.commit()


@contextmanager
def get_main_db():
    """只读连接主库（用于查询 users/order_items/api_logs/admin_actions 表）。"""
    from models import get_db as main_get_db
    with main_get_db() as conn:
        yield conn


def get_schema_version() -> str:
    """从 schema_meta 表读取当前 schema 版本（§10.6）。"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return row['value'] if row else '0.0.0'
    except Exception:
        return '0.0.0'


def set_schema_version(version: str):
    """写入当前 schema 版本（§10.6）。"""
    with get_db() as conn:
        conn.execute('''
            INSERT INTO schema_meta (key, value, updated_at)
            VALUES ('schema_version', ?, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        ''', (version,))
        conn.commit()
