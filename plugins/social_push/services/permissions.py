#!/usr/bin/env python3
"""Social Push Plugin — 权限控制（P6）

基于 JWT role 声明（super_admin / admin / operator / user）做发布权限门禁。
沿用系统既有角色体系，不新建表、不引入新配置。

权限动作：
  read    — 查看历史 / 队列 / 草稿（所有已登录管理员）
  write   — 保存草稿 / 删除草稿 / 取消队列任务
  publish — 执行发布 / 入队定时发布
"""
import logging

from flask import request, jsonify
from i18n import _

logger = logging.getLogger(__name__)

# 各动作允许的角色
_PERMISSION_ROLES = {
    'read':    {'super_admin', 'admin', 'operator', 'user'},
    'write':   {'super_admin', 'admin', 'operator'},
    'publish': {'super_admin', 'admin', 'operator'},
}


def get_admin_role() -> str:
    """从当前请求 JWT 解码角色（默认 'user'）。"""
    try:
        from services.jwt_service import validate_token
    except Exception:
        return 'user'
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    return (payload or {}).get('role', 'user')


def check_permission(action: str):
    """权限门禁：无权限返回 403 响应，有权限返回 None。"""
    role = get_admin_role()
    allowed = _PERMISSION_ROLES.get(action, set())
    if role in allowed:
        return None
    return jsonify({'success': False,
                    'error': _('No permission for this operation')}), 403
