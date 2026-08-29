"""订单通知独立数据库模型"""
from flask import g
from plugins._base.db import PgConnection, get_raw_connection


def get_db():
    """获取本插件的独立数据库连接"""
    if 'order_notify_db' not in g:
        raw = get_raw_connection()
        cur = raw.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS order_notify")
        cur.execute("SET search_path TO order_notify")
        raw.commit()
        cur.close()
        g.order_notify_db = raw
    return PgConnection(g.order_notify_db)


def get_main_db():
    """只读访问主库（用于查询 order_items 等数据）"""
    from models import get_db as main_db
    return main_db()


def init_db():
    """初始化插件自有表"""
    from plugins._base.db import get_raw_connection
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS order_notify")
    cur.execute("SET search_path TO order_notify")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notification_log (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            event       TEXT    NOT NULL,
            order_id    TEXT    NOT NULL,
            title       TEXT    NOT NULL DEFAULT '',
            content     TEXT    NOT NULL DEFAULT '',
            link_url    TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_notification_log_user
            ON notification_log(user_id)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_notification_log_order
            ON notification_log(order_id)
    """)
    conn.commit()
    cur.close()
    conn.close()


def close_db(exception=None):
    db = g.pop('order_notify_db', None)
    if db is not None:
        db.close()
