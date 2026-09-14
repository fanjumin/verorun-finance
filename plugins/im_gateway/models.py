#!/usr/bin/env python3
"""IM Gateway Plugin — 数据库模型

独立数据库 im_gateway.db，存放频道配置表 channel_configs。
从主库迁移而来（feishu/wecom/qq/dingtalk），主库结构保持一致。
"""
import os
import psycopg2
from plugins._base.db import get_pooled_connection

# 默认频道种子（channel, is_enabled）
_SEED_CHANNELS = [
    ('feishu', 1),
    ('wecom', 1),
    ('qq', 0),
    ('dingtalk', 0),
]


def get_im_db():
    """每次从共享池借取连接并设置 im_gateway schema（用完必须 close() 归还池）"""
    conn = None
    try:
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS im_gateway")
        conn.execute("SET search_path TO im_gateway")
        conn.execute("SELECT 1").fetchone()
        return conn
    except psycopg2.DatabaseError as e:
        if conn is not None:
            try:
                conn.close()  # 归还（坏连接由池丢弃）
            except Exception:
                pass
        print(f'[IMGateway] ⚠️ Database damaged, reconnect: {e}')
        conn = get_pooled_connection()
        conn.execute("CREATE SCHEMA IF NOT EXISTS im_gateway")
        conn.execute("SET search_path TO im_gateway")
        return conn


def init_im_db():
    """初始化频道配置表 + 种子数据（幂等）。

    表结构与主库 channel_configs 保持完全一致，便于数据迁移。
    """
    with get_im_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_configs (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                channel         TEXT NOT NULL UNIQUE,
                config_json     TEXT NOT NULL DEFAULT '{}',
                is_enabled      BIGINT DEFAULT 0,
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            )
        """)
        # 跨 worker 频控计数表（gateway._rate_limited 使用），幂等创建
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                channel     TEXT NOT NULL,
                ts          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rate_limit_events_channel_ts
                ON rate_limit_events(channel, ts)
        """)
        # 第三方登录提供方配置表（Phase 5，方案 A：插件自包含）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_providers (
                id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                provider        TEXT NOT NULL UNIQUE,
                display_name    TEXT NOT NULL DEFAULT '',
                client_id       TEXT NOT NULL DEFAULT '',
                client_secret   TEXT NOT NULL DEFAULT '',
                scopes          TEXT NOT NULL DEFAULT '',
                redirect_uri    TEXT NOT NULL DEFAULT '',
                is_enabled      BIGINT DEFAULT 0,
                created_at      TEXT DEFAULT NOW(),
                updated_at      TEXT DEFAULT NOW()
            )
        """)
        # 第三方登录用户绑定表（Phase 5，方案 B：统一走绑定表，联邦身份）
        # 五平台（wechat/qq/weibo/github/google）登录用户统一落此表，
        # 不做主库 users 结构扩展；JWT 仍由 auth-center 签发。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_user_bindings (
                id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                provider         TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                user_id          BIGINT NOT NULL,
                username         TEXT NOT NULL DEFAULT '',
                display_name     TEXT NOT NULL DEFAULT '',
                avatar           TEXT NOT NULL DEFAULT '',
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                updated_at       TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_login_bindings UNIQUE (provider, provider_user_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_login_bindings_user_id
                ON login_user_bindings(user_id)
        """)
        # 第三方登录 CSRF state 表（一次性、10 分钟过期）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_login_states (
                id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                state      TEXT NOT NULL UNIQUE,
                provider   TEXT NOT NULL DEFAULT '',
                consumed   BIGINT DEFAULT 0,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for channel, is_enabled in _SEED_CHANNELS:
            exists = conn.execute(
                "SELECT id FROM channel_configs WHERE channel=%s", (channel,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO channel_configs (channel, config_json, is_enabled) VALUES (%s, '{}', %s)",
                    (channel, is_enabled)
                )
        conn.commit()


def migrate_from_main_db():
    """从主库 channel_configs 迁移已有配置到插件库（幂等）。

    仅当插件库对应频道 config_json 为空 '{}' 时才覆盖，避免回退用户新改的配置。
    主库无 channel_configs 表或无数据时静默跳过。
    返回迁移的记录数。
    """
    try:
        from models import get_db as get_main_db
    except Exception:
        return 0

    migrated = 0
    try:
        with get_main_db() as main:
            has_table = main.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE tablename='channel_configs'"
            ).fetchone()
            if not has_table:
                return 0
            rows = main.execute(
                "SELECT channel, config_json, is_enabled FROM channel_configs"
            ).fetchall()
    except Exception:
        return 0

    with get_im_db() as conn:
        for r in rows:
            channel = r['channel']
            config_json = r['config_json'] or '{}'
            is_enabled = r['is_enabled']
            local = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel=%s", (channel,)
            ).fetchone()
            if local is None:
                conn.execute(
                    "INSERT INTO channel_configs (channel, config_json, is_enabled) VALUES (%s, %s, %s)",
                    (channel, config_json, is_enabled)
                )
                migrated += 1
            elif (local['config_json'] or '{}') == '{}' and config_json != '{}':
                conn.execute(
                    "UPDATE channel_configs SET config_json=%s, is_enabled=%s, updated_at=NOW() WHERE channel=%s",
                    (config_json, is_enabled, channel)
                )
                migrated += 1
        conn.commit()
    return migrated
