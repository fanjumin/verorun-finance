#!/usr/bin/env python3
"""site_builder — 标准连接工厂（§9.1/§11.2 统一连接池）。

2026-08-21 取消独立数据库豁免：插件数据回归主库 appdb 独立 schema
`site_builder`，连接统一走 plugins/_base/db.py 的共享连接池
（get_pooled_connection），与其余插件同标准。

兼容导出（调用方零改动）：
    get_raw_connection()   —— 一次性直连主库（卸载清理用）
    get_db()               —— 借池 + SET search_path TO site_builder, public
"""

from plugins._base.db import (
    get_raw_connection as _base_raw_connection,
    get_pooled_connection,
)


def get_raw_connection():
    """一次性直连主库 appdb（on_uninstall 等一次性清理场景），用完即关。

    标准 §9.1/§11.2 例外：卸载清理不借池，避免池内残留半关闭事务。
    """
    return _base_raw_connection()


def get_db():
    """从共享连接池借取主库连接并切换到 site_builder schema（标准 §9.1/§11.2）。

    返回 plugins._base.db.PgConnection（池化连接）：with 退出即归还池，
    归还前自动 rollback + 重置 search_path TO public，不污染池内连接。
    调用方一律 with get_db() as conn: 使用。
    """
    conn = get_pooled_connection()
    conn.execute("SET search_path TO site_builder, public")
    return conn
