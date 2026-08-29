# -*- coding: utf-8 -*-
"""Platform user registry — 平台登录用户注册核心服务 (v2.1.0).

小程序模块数据库解耦后，插件不再直接访问主库 users 表。本模块作为
auth-center 提供的**共享服务层**，负责在系统主库中「查找或创建」平台登录
用户，供 mini_app_builder 插件（及其他需要平台登录的服务）函数内懒加载调用。

各平台在主库 users 表使用专用身份列：
    douyin   -> douyin_open_id
    wechat   -> wechat_openid
    telegram -> telegram_open_id
    line     -> 无专用列，按 username 唯一匹配

插件侧登录成功后，会将 平台身份 -> 主库 user_id 的映射写入独立库
platform_users.platform_user_mappings（见 plugins/mini_app_builder/platform_users.py），
实现联邦身份：JWT 仍由 auth-center 签发，跨服务共享。
"""

from models import get_db


# 平台 -> users 表身份列（None 表示无专用列，按 username 匹配）
_PLATFORM_ID_COLS = {
    'douyin': 'douyin_open_id',
    'wechat': 'wechat_openid',
    'telegram': 'telegram_open_id',
    'line': None,
}

_SELECT_COLS = 'id, username, display_name, avatar_url AS avatar, platform'  # platform 保留占位（users 无该列）
_SELECT_CORE = 'id, username, display_name, avatar_url AS avatar'


def register_or_get_platform_user(platform: str, platform_user_id: str,
                                  username: str, display_name: str,
                                  avatar: str = '') -> dict:
    """在主库中查找或创建平台用户（get-or-create）。

    Args:
        platform: douyin / wechat / telegram / line
        platform_user_id: 平台侧用户标识（openid / tg user id / line userId）
        username: 系统内唯一登录名（调用方生成，如 wx_xxxx / tg_xxxx）
        display_name: 显示名
        avatar: 头像 URL（可选）

    Returns:
        {'id', 'username', 'display_name', 'avatar'} + 未加密平台 id 列
    """
    id_col = _PLATFORM_ID_COLS.get(platform)
    if not id_col and platform not in _PLATFORM_ID_COLS:
        raise ValueError(f'Unsupported platform: {platform}')

    with get_db() as conn:
        if id_col:
            row = conn.execute(
                f"SELECT id, username, display_name, avatar_url FROM users WHERE {id_col}=%s",
                (platform_user_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    f"INSERT INTO users (username, display_name, {id_col}, avatar_url, created_at, last_login) "
                    f"VALUES (%s, %s, %s, %s, NOW(), NOW())",
                    (username, display_name, platform_user_id, avatar or '')
                )
                conn.commit()
                row = conn.execute(
                    f"SELECT id, username, display_name, avatar_url FROM users WHERE {id_col}=%s",
                    (platform_user_id,)
                ).fetchone()
            else:
                # 已存在：静默更新昵称/头像（仅当提供了新值时）
                if avatar or display_name:
                    conn.execute(
                        "UPDATE users SET last_login=NOW(), "
                        "avatar_url=COALESCE(NULLIF(%s,''), avatar_url), "
                        "display_name=COALESCE(NULLIF(%s,''), display_name) "
                        "WHERE id=%s",
                        (avatar or '', display_name or '', row['id'])
                    )
                    conn.commit()
        else:
            # line 等无专用列平台：按 username 匹配
            row = conn.execute(
                "SELECT id, username, display_name, avatar_url FROM users WHERE username=%s",
                (username,)
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO users (username, display_name, avatar_url, created_at, last_login) "
                    "VALUES (%s, %s, %s, NOW(), NOW())",
                    (username, display_name, avatar or '')
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id, username, display_name, avatar_url FROM users WHERE username=%s",
                    (username,)
                ).fetchone()
            else:
                conn.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (row['id'],))
                conn.commit()

        return dict(row)


def get_user_by_id(user_id: int) -> dict | None:
    """按主库 user_id 查询用户基础信息（供插件 /user/profile 使用）。"""
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, avatar_url, created_at "
            "FROM users WHERE id=%s",
            (user_id,)
        ).fetchone()
    return dict(row) if row else None
