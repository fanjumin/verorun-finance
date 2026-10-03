#!/usr/bin/env python3
"""project_workspace/auth.py — 鉴权守卫。

- current_user(): 从 Authorization: Bearer <token> / sso_token / tm_token cookie 解析 JWT
- require_user: 未登录返回 401 {'ok': False, 'error': 'Please login first'}
- require_project_role(min_role): 按 project_id 校验成员身份与角色级别

角色级别：viewer=1, editor=2, owner=3（存量数据 role='member' 按 editor 处理）。
校验通过后将 request._project_id 与 request._project_role 注入请求上下文。
"""

import os
import sys
import uuid
from functools import wraps

from flask import current_app, request, jsonify

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTH_CENTER_DIR = os.path.join(_THIS_DIR, '..', '..', 'auth-center')
if _AUTH_CENTER_DIR not in sys.path:
    sys.path.insert(0, _AUTH_CENTER_DIR)

ROLE_LEVELS = {'viewer': 1, 'editor': 2, 'owner': 3}
# 存量数据兼容：旧 role 值 → 新角色
_LEGACY_ROLE_MAP = {'member': 'editor'}


# -- i18n helpers --------------------------------------------------------

def _normalize_locale(value) -> str:
    """将 Accept-Language / ?lang 值规范化为 'en' / 'zh-CN'，未知返回 None。"""
    value = (value or '').strip().lower().replace('_', '-')
    if not value:
        return None
    if value.startswith('zh'):
        return 'zh-CN'
    if value.startswith('en'):
        return 'en'
    return None


def _get_locale() -> str:
    """解析当前请求语言：?lang= 优先 → Accept-Language 首项 → DEPLOY_LANG → 'en'。"""
    loc = _normalize_locale(request.args.get('lang'))
    if loc:
        return loc
    header = request.headers.get('Accept-Language') or ''
    for part in header.split(','):
        loc = _normalize_locale(part.split(';')[0])
        if loc:
            return loc
    try:
        from i18n import get_lang
        loc = _normalize_locale(get_lang())
        if loc:
            return loc
    except Exception:
        pass
    return 'en'


def _t(key: str) -> str:
    """从插件 i18n 读取当前语言的翻译；任何失败回退 key 原文。"""
    try:
        pm = current_app.extensions.get('plugin_manager')
        instance = pm.get_instance('project_workspace') if pm else None
        if instance is not None:
            return instance.t(key, locale=_get_locale())
    except Exception:
        pass
    return key


def _validate(token):
    from services.jwt_service import validate_token
    return validate_token(token) if token else None


def current_user():
    """从请求解析当前登录用户（JWT payload dict），未登录返回 None。"""
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    try:
        return _validate(token)
    except Exception:
        return None


def _json_data():
    return request.get_json(silent=True) or {}


def _normalize_uid(value):
    return '' if value is None else str(value)


def _is_uuid(value) -> bool:
    """路径参数中的 id 必须是合法 UUID。

    projects / documents / qa_logs 的主键均为 uuid 类型；非 UUID（如 "1"）
    直接交给 ``WHERE id = ?`` 会让 PostgreSQL 抛
    ``invalid input syntax for type uuid``，进而变成 500。此处提前判定，
    按“不存在”处理。
    """
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _resolve_project_id(kwargs: dict):
    """解析目标 project_id：优先 kwargs → args → json，其次从 doc/qa 记录反查。"""
    pid = kwargs.get('project_id') or ''
    if not pid:
        pid = request.args.get('project_id', '')
    if not pid:
        pid = _json_data().get('project_id', '')
    if pid:
        return str(pid)

    # 反查：doc_id/document_id → documents.project_id（文档级操作的项目隔离）
    data = _json_data()
    ref_id = (kwargs.get('doc_id') or kwargs.get('document_id') or
              request.args.get('doc_id') or request.args.get('document_id') or
              data.get('document_id') or data.get('doc_id'))
    if not ref_id:
        ids = data.get('document_ids')
        if isinstance(ids, list) and ids:
            ref_id = str(ids[0])
    if ref_id:
        if not _is_uuid(ref_id):
            return None
        from .models import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT project_id FROM documents WHERE id = ?", (str(ref_id),)
            ).fetchone()
            if row:
                return str(row['project_id'])
        finally:
            conn.close()
        return None

    # 反查：qa_id → qa_logs.project_id（问答反馈的项目隔离）
    qa_id = (kwargs.get('qa_id') or request.args.get('qa_id') or
             _json_data().get('qa_id'))
    if qa_id:
        if not _is_uuid(qa_id):
            return None
        from .models import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT project_id FROM qa_logs WHERE id = ?", (str(qa_id),)
            ).fetchone()
            if row:
                return str(row['project_id'])
        finally:
            conn.close()
    return None


def _fetch_project_role(project_id: str, user_id: str):
    """返回 'missing'（项目不存在）/ 'forbidden'（非成员）/ 角色名。"""
    if not _is_uuid(project_id):
        return 'missing'
    from .models import get_db
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT owner_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not row:
            return 'missing'
        if _normalize_uid(row['owner_id']) == user_id:
            return 'owner'
        mrow = conn.execute(
            "SELECT role FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id)
        ).fetchone()
        if not mrow:
            return 'forbidden'
        return _LEGACY_ROLE_MAP.get(mrow['role'], mrow['role'])
    finally:
        conn.close()


def require_user(f):
    """登录守卫：未登录返回 401。登录后将 payload 注入 request._user。"""

    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({'ok': False, 'error': _t('auth.error_login_required')}), 401
        request._user = user
        return f(*args, **kwargs)

    return wrapper


def require_project_role(min_role='viewer'):
    """项目角色守卫工厂。

    Args:
        min_role: 'viewer' | 'editor' | 'owner'

    校验通过后将 request._project_id 与 request._project_role 注入请求上下文。
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = getattr(request, '_user', None)
            if not user:
                return jsonify({'ok': False, 'error': _t('auth.error_login_required')}), 401
            project_id = _resolve_project_id(kwargs)
            if not project_id:
                return jsonify({'ok': False, 'error': _t('auth.error_project_required')}), 400
            result = _fetch_project_role(project_id, _normalize_uid(user.get('user_id')))
            if result == 'missing':
                return jsonify({'ok': False, 'error': _t('auth.error_project_not_found')}), 404
            if result == 'forbidden':
                return jsonify({'ok': False, 'error': _t('auth.error_no_access')}), 403
            if ROLE_LEVELS.get(result, 1) < ROLE_LEVELS.get(min_role, 1):
                return jsonify({'ok': False, 'error': _t('auth.error_insufficient_role')}), 403
            request._project_id = project_id
            request._project_role = result
            return f(*args, **kwargs)

        return wrapper

    return decorator
