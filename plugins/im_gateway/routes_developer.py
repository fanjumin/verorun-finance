#!/usr/bin/env python3
"""开发者登录 — API Key 管理端点（Phase 4）。

复用 auth-center UnifiedAuthService（只读/签发/吊销），不建表、不直写库。
"""
import logging
import os
import sys

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, jsonify, request  # noqa: E402

logger = logging.getLogger(__name__)

developer_bp = Blueprint('im_gateway_developer', __name__,
                         url_prefix='/admin/channels/developer')


def _require_admin():
    """复用主系统管理员鉴权（与 routes.py 一致）"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _svc():
    from services.unified_auth_service import UnifiedAuthService
    return UnifiedAuthService()


@developer_bp.route('/keys/issue', methods=['POST'])
def issue_key():
    """签发新的开发者 API Key（raw_key 仅返回一次，调用方需立即展示）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    key_type = data.get('key_type', 'user')
    if key_type not in ('user', 'agent', 'provider'):
        return jsonify({'success': False, 'error': 'invalid key_type'}), 400
    try:
        raw_key, info = _svc().generate_key(
            user_id=admin['user_id'],
            key_type=key_type,
            name=data.get('name', '') or '',
            expire_at=data.get('expire_at') or None,
        )
    except Exception as e:
        logger.exception('[Developer] issue key failed')
        return jsonify({'success': False, 'error': str(e)[:2000]}), 500
    return jsonify({'success': True, 'raw_key': raw_key, 'data': info})


@developer_bp.route('/keys/revoke/<int:key_id>', methods=['POST'])
def revoke_key(key_id):
    """吊销开发者 API Key（软删除，仅限本人）"""
    admin, err = _require_admin()
    if err:
        return err
    try:
        ok = _svc().revoke_key(key_id, admin['user_id'])
    except Exception as e:
        logger.exception('[Developer] revoke key failed')
        return jsonify({'success': False, 'error': str(e)[:2000]}), 500
    if not ok:
        return jsonify({'success': False, 'error': 'key not found'}), 404
    return jsonify({'success': True})
