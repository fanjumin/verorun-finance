"""订单通知独立数据库模型"""
from contextlib import contextmanager
from plugins._base.db import get_pooled_connection


@contextmanager
def get_db():
    """获取本插件独立数据库连接（共享连接池，用完自动归还）。"""
    with get_pooled_connection() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS order_notify")
        conn.execute("SET search_path TO order_notify")
        yield conn


def get_main_db():
    """只读访问主库（用于查询 order_items 等数据）"""
    from models import get_db as main_db
    return main_db()


def init_db():
    """初始化插件自有表"""
    from plugins._base.db import get_pooled_connection
    with get_pooled_connection() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS order_notify")
        conn.execute("SET search_path TO order_notify")
        conn.execute("""
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
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_log_user
                ON notification_log(user_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_log_order
                ON notification_log(order_id)
        """)
