#!/usr/bin/env python3
"""
Email Plugin Models — PostgreSQL schema: email
===============================================
完全独立于主库，使用独立 PG schema。
"""

from i18n import _
import psycopg2
from plugins._base.db import get_pooled_connection


def get_email_db():
    """每次从共享池借取连接并设置 email schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS email")
        conn.execute("SET search_path TO email")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(_('[EmailPlugin] ⚠️ Database damaged, reconnect: {}').format(e))
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS email")
        conn.execute("SET search_path TO email")
        return conn


def init_email_db():
    """初始化邮件插件数据库表（幂等）"""
    with get_email_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS email_sent (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            from_addr       TEXT NOT NULL,
            to_addr         TEXT NOT NULL,
            subject         TEXT NOT NULL,
            body_text       TEXT,
            body_html       TEXT,
            in_reply_to     BIGINT,
            sent_at         TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_email_sent_from ON email_sent(from_addr)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_email_sent_sent_at ON email_sent(sent_at)')
        conn.commit()
        print(_('[EmailPlugin] PG schema email initialized'))


# 兼容旧接口名
ensure_email_tables = init_email_db