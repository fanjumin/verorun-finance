#!/usr/bin/env python3
"""
Logistics Plugin Models — 独立 PG schema `logistics`
==================================================
完全独立于主库。
- logistics_queries: 物流查询日志
"""
from i18n import _
import psycopg2
from plugins._base.db import get_pooled_connection
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('logistics')


def get_logistics_db():
    """每次从共享池借取连接并设置 logistics schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS logistics")
        conn.execute("SET search_path TO logistics")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[LogisticsPlugin] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS logistics")
        conn.execute("SET search_path TO logistics")
        return conn


def init_logistics_db():
    """初始化物流插件数据库表（幂等）"""
    with get_logistics_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS logistics_queries (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            shipper_code    TEXT NOT NULL,
            logistic_code   TEXT NOT NULL,
            order_code      TEXT DEFAULT '',
            success         BIGINT DEFAULT 0,
            error_msg       TEXT DEFAULT '',
            queried_at      TEXT DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logistics_queries_code ON logistics_queries(logistic_code)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logistics_queries_at ON logistics_queries(queried_at)')
        conn.commit()
        logger.info(_('LogisticsPlugin database initialized'))


# 兼容旧接口名
ensure_logistics_tables = init_logistics_db
