#!/usr/bin/env python3
"""
Currency Converter Plugin Models — PostgreSQL schema: currency_converter
=========================================================================
汇率 + 用户币种偏好，完全独立于主库。
"""
import psycopg2
from plugins._base.db import get_pooled_connection


def get_db():
    """每次从共享池借取连接并设置 currency_converter schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS currency_converter")
        conn.execute("SET search_path TO currency_converter")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[CurrencyConverter] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS currency_converter")
        conn.execute("SET search_path TO currency_converter")
        return conn


def init_db():
    """创建插件数据库表（幂等）"""
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS exchange_rates (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            currency_code   TEXT UNIQUE NOT NULL,
            rate_to_base    DOUBLE PRECISION NOT NULL,
            base_currency   TEXT NOT NULL DEFAULT 'CNY',
            source          TEXT DEFAULT '',
            fetched_at      TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS user_currency_prefs (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id             BIGINT UNIQUE NOT NULL,
            preferred_currency  TEXT NOT NULL DEFAULT 'CNY',
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_exchange_code ON exchange_rates(currency_code)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_user_pref ON user_currency_prefs(user_id)')
        conn.commit()
        print('[CurrencyConverter] PG schema currency_converter initialized')
