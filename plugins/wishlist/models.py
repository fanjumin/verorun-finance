"""
Wishlist Plugin — 基于 PostgreSQL schema 的数据层
==================================================
使用独立 PG schema `wishlist`，主库只读 products 表。
"""

from contextlib import contextmanager
from plugins._base.db import PgConnection, get_raw_connection


@contextmanager
def get_db():
    """连接插件自己的数据库（PG schema: wishlist）。"""
    conn = get_raw_connection()
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS wishlist")
    cur.execute("SET search_path TO wishlist, public")
    cur.close()
    try:
        yield PgConnection(conn)
    finally:
        conn.close()


def init_db():
    """创建插件自己的表（幂等）。"""
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS wishlist (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                product_id  BIGINT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, product_id)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_wishlist_user ON wishlist(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_wishlist_product ON wishlist(product_id)')
        conn.commit()


@contextmanager
def get_main_db():
    """只读连接主库（用于查询 products 表）。"""
    from models import get_db as main_get_db
    with main_get_db() as conn:
        yield conn
