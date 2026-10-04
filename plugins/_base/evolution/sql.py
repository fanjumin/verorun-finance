#!/usr/bin/env python3
"""跨库读取 public.user_profiles.meta 的唯一真源。

统一以 %s 绑定 auth-center get_db，杜绝各插件再各写一遍 get_db()+占位符。
只读、不含域策略：默认值、fail-open / fail-closed 由各插件包装层决定。
"""

import json


def user_profile_meta(user_id):
    """读取 public.user_profiles.meta。

    Returns:
        {}    —— 无该用户档案
        dict  —— 用户档案 meta
        None  —— 读失败 / meta 非法（坏 json / 非 dict）
    """
    try:
        from agent_matrix.models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT meta FROM public.user_profiles WHERE user_id = %s",
                (user_id,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return {}
    meta = row['meta'] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return None
    return meta if isinstance(meta, dict) else None
