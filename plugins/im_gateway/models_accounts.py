#!/usr/bin/env python3
"""统一网关 — 渠道账号表（Phase 1，加密存储）。

社媒 OAuth token / 客户端凭据集中加密存储于 channel_accounts 表，
与旧 channel_configs（明文）并存，互不干扰。
复用 models.get_im_db() 的连接（同 schema im_gateway），不新增连接方式。
"""
import json

from .models import get_im_db
from .crypto import encrypt, decrypt

_table_ready = False


def _ensure_accounts_table():
    """惰性幂等建表（首次调用任何函数时执行，避免改动 __init__.py 生命周期）"""
    global _table_ready
    if _table_ready:
        return
    with get_im_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_accounts (
                id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                channel          TEXT NOT NULL,
                account_key      TEXT NOT NULL,          -- 平台 uid/page id 等
                handle           TEXT DEFAULT '',
                config_json      TEXT NOT NULL DEFAULT '{}',  -- 敏感字段整体加密后整存
                token_expires_at TEXT DEFAULT '',
                is_enabled       BIGINT DEFAULT 1,
                created_at       TEXT DEFAULT NOW(),
                updated_at       TEXT DEFAULT NOW(),
                UNIQUE (channel, account_key)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_accounts_channel
                ON channel_accounts(channel)
        """)
        conn.commit()
    _table_ready = True


def save_account(channel: str, account_key: str, config: dict,
                 handle: str = '', token_expires_at: str = '',
                 is_enabled: int = 1) -> int:
    """保存账号；config 整体加密后整存（access_token/refresh_token/app_secret 等）"""
    _ensure_accounts_table()
    encrypted = encrypt(json.dumps(config, ensure_ascii=False))
    with get_im_db() as conn:
        cur = conn.execute("""
            INSERT INTO channel_accounts
                (channel, account_key, handle, config_json, token_expires_at, is_enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (channel, account_key) DO UPDATE SET
                handle=EXCLUDED.handle,
                config_json=EXCLUDED.config_json,
                token_expires_at=EXCLUDED.token_expires_at,
                is_enabled=EXCLUDED.is_enabled,
                updated_at=NOW()
            RETURNING id
        """, (channel, account_key, handle, encrypted, token_expires_at, is_enabled))
        conn.commit()
        return cur.fetchone()['id']


def get_account(channel: str, account_key: str) -> dict | None:
    """读取账号并解密"""
    _ensure_accounts_table()
    with get_im_db() as conn:
        row = conn.execute(
            "SELECT * FROM channel_accounts WHERE channel=%s AND account_key=%s",
            (channel, account_key)
        ).fetchone()
    if not row:
        return None
    cfg = json.loads(decrypt(row['config_json'] or '{}'))
    return {**dict(row), 'config': cfg}


def list_accounts(channel: str = None) -> list:
    """列账号（解密），channel 可空"""
    _ensure_accounts_table()
    with get_im_db() as conn:
        if channel:
            rows = conn.execute(
                "SELECT * FROM channel_accounts WHERE channel=%s ORDER BY id", (channel,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM channel_accounts ORDER BY id").fetchall()
    result = []
    for r in rows:
        result.append({**dict(r), 'config': json.loads(decrypt(r['config_json'] or '{}'))})
    return result


def delete_account(account_id: int) -> bool:
    """删除账号（撤销授权用）"""
    _ensure_accounts_table()
    with get_im_db() as conn:
        conn.execute("DELETE FROM channel_accounts WHERE id=%s", (account_id,))
        conn.commit()
    return True
