"""知识库权限检查模块 — 统一入口，供所有知识库 API 使用"""
from flask import jsonify
from i18n import _


def get_admin_role(token_payload: dict) -> str:
    """从 JWT payload 中提取角色。
    返回: 'super_admin' | 'admin' | 'operator' | 'user'
    """
    return token_payload.get('role', 'user')


def check_kb_permission(scope: str, owner_id: int, action: str,
                        token_payload: dict) -> tuple:
    """检查当前用户对知识库条目的操作权限。

    参数:
        scope:  'system' | 'user'
        owner_id: 知识库条目的 owner_id
        action: 'read' | 'write' | 'delete' | 'update_system'
        token_payload: JWT 解码后的 payload

    返回: (True, None) 或 (False, error_response)
    """
    user_id = token_payload.get('user_id')
    role = get_admin_role(token_payload)

    if action == 'read':
        # 读操作：所有登录用户均可读系统KB和用户KB
        return True, None

    if scope == 'system':
        if action == 'update_system':
            # 系统在线更新：仅超级管理员
            if role == 'super_admin':
                return True, None
            return False, (jsonify({
                'success': False,
                'error': _('System knowledge base online update can only be performed by super admin')
            }), 403)

        if action == 'write':
            # 系统KB写入：仅超级管理员
            if role == 'super_admin':
                return True, None
            return False, (jsonify({
                'success': False,
                'error': '系统知识库仅超级管理员可修改'
            }), 403)

        if action == 'delete':
            # 系统KB删除：完全禁止
            return False, (jsonify({
                'success': False,
                'error': '系统知识库禁止删除。如需更新请使用在线更新功能'
            }), 403)

    elif scope == 'user':
        if role in ('super_admin', 'admin'):
            # 管理员可以操作所有用户KB
            return True, None

        if action in ('read', 'write', 'delete'):
            if owner_id == user_id:
                return True, None
            return False, (jsonify({
                'success': False,
                'error': '仅可操作自己的知识库条目'
            }), 403)

    return False, (jsonify({
        'success': False,
        'error': _('Unknown knowledge base scope')
    }), 400)


# ═══ 科研版扩展：密级判定（纯逻辑，无 DB 依赖）═══════════
# 密级 -> 最小可用机构角色（super_admin 全通过）
_CONF_ROLE = {
    'public':   ('super_admin', 'admin', 'operator', 'user'),
    'internal': ('super_admin', 'admin', 'operator'),
    'secret':   ('super_admin',),
}


def check_confidentiality(conf: str, role: str, membership: str = '') -> bool:
    """科研密级判定：密级 + 机构角色 + 项目成员角色 双条件。

    参数:
        conf:       'public' | 'internal' | 'secret'（缺省回落 'internal'）
        role:       机构角色 'super_admin'|'admin'|'operator'|'user'
        membership: 项目成员角色（'owner'|'member'|'reviewer'|'viewer' 或 ''）
    返回: 是否允许访问
    """
    conf = (conf or 'internal').strip().lower()
    if conf not in _CONF_ROLE:
        conf = 'internal'
    if role in _CONF_ROLE[conf]:
        return True
    # 公开条目：项目成员（含外部访客）可只读协作
    if conf == 'public' and membership in ('owner', 'member', 'reviewer', 'viewer'):
        return True
    return False


def is_project_member(project_id, user_id, lookup) -> str:
    """查询用户的项目成员角色（由调用方注入查询实现，保持解耦）。

    参数:
        project_id: 项目 UUID 或 None
        user_id:    用户 ID
        lookup:     lookup(project_id, user_id) -> 成员角色字符串或 ''
    返回: 成员角色字符串，或 ''（非成员/查询异常）
    """
    if not project_id or not user_id:
        return ''
    try:
        return lookup(project_id, user_id) or ''
    except Exception:
        return ''
