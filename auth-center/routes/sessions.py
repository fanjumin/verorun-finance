#!/usr/bin/env python3
"""Session Routes — user login session management.
   
   Tracks active login sessions, supports viewing current devices
   and remotely logging out sessions.
   
   New architecture (2026-05-10):
   - user_sessions table: tracks each login session (device, IP, token hash)
   - Allows users to see and manage their active sessions
"""
from i18n import _
import sys, os, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import Blueprint, request, jsonify
from models import get_db
from services.jwt_service import validate_token

session_bp = Blueprint('session', __name__, url_prefix='/session')


def _require_auth():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    payload = validate_token(token)
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Not logged in or token expired')}), 401)
    return payload, None


# =============================================
# GET /session/list — list active sessions
# =============================================
@session_bp.route('/list', methods=['GET'])
def session_list():
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, device_name, device_type, ip_address, user_agent, "
                "       location, is_current, created_at "
                "FROM user_sessions WHERE user_id=%s AND (expired_at IS NULL OR expired_at > NOW()) "
                "ORDER BY is_current DESC, created_at DESC",
                (uid,)
            ).fetchall()
        return jsonify({'success': True, 'data': [dict(r) for r in rows]})
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500


# =============================================
# GET /session/current — current session info
# =============================================
@session_bp.route('/current', methods=['GET'])
def session_current():
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    # Get current token hash
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, device_name, device_type, ip_address, user_agent, "
                "       location, created_at "
                "FROM user_sessions WHERE user_id=%s AND token_hash=%s",
                (uid, token_hash)
            ).fetchone()
            if not row:
                # Record this as a new session
                user_agent = request.headers.get('User-Agent', '')[:256]
                ip = request.remote_addr or ''
                sid = conn.execute(
                    "INSERT INTO user_sessions (user_id, token_hash, device_type, ip_address, user_agent, is_current) "
                    "VALUES (%s,%s,%s,%s,%s,1) RETURNING id",
                    (uid, token_hash, 'api', ip, user_agent)
                ).fetchone()['id']
                conn.commit()
                return jsonify({'success': True, 'data': {
                    'id': sid,
                    'device_type': 'api',
                    'ip_address': ip,
                    'user_agent': user_agent,
                    'is_new': True,
                }})
    
        return jsonify({'success': True, 'data': dict(row)})
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500


# =============================================
# DELETE /session/<id> — logout/terminate a session
# =============================================
@session_bp.route('/<int:sid>', methods=['DELETE'])
def session_terminate(sid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, is_current FROM user_sessions WHERE id=%s AND user_id=%s",
                (sid, uid)
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': _('Session does not exist')}), 404
            if row['is_current']:
                return jsonify({'success': False, 'error': '不能退出当前会话，请使用退出登录'}), 400
            conn.execute(
                "UPDATE user_sessions SET expired_at=NOW() WHERE id=%s",
                (sid,)
            )
            conn.commit()
        return jsonify({'success': True, 'message': _('Session has been terminated')})
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
