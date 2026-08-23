#!/usr/bin/env python3
"""OAuth Plugin — 主库数据模型（§12.10）

oauth_providers 表收敛至主库 public schema，由 init_oauth_tables() 幂等创建。
admin/services/auth 统一通过主库连接读写，不再使用独立 schema。
"""
from contextlib import contextmanager

# §10.5 standard logging — safe: __init__.py defines _plugin_log at module level, no circular import
from plugins.oauth_config import _plugin_log


@contextmanager
def get_db():
    """获取主库连接（§12.10 — oauth_providers 共享主库 public schema）"""
    from models import get_db as _main_get_db
    with _main_get_db() as conn:
        yield conn


def init_oauth_tables():
    """在主库 public schema 创建 oauth_providers 表（幂等）"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_providers (
                id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                site_domain   TEXT NOT NULL,
                provider      TEXT NOT NULL DEFAULT 'douyin',
                client_key    TEXT NOT NULL DEFAULT '',
                client_secret TEXT NOT NULL DEFAULT '',
                is_active     BIGINT NOT NULL DEFAULT 1,
                created_at    TEXT,
                updated_at    TEXT,
                UNIQUE(site_domain, provider)
            )
        """)
        conn.commit()
    _plugin_log('[OAuthPlugin] oauth_providers table is ready in main DB (public schema)')
