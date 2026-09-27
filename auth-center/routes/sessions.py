#!/usr/bin/env python3
"""Session Routes — user login session management.
   
   Tracks active login sessions, supports viewing current devices
   and remotely logging out sessions.
   
   New architecture (2026-05-10):
   - user_sessions table: tracks each login session (device, IP, token hash)
   - Allows users to see and manage their active sessions

   会话缓存上行（2026-09-25，B-3）：
   - POST /session/sync                — 桌面「本地会话缓存」全量上行，返回服务端 id
   - GET  /session/<int:sid>/messages  — 读回已上行会话的消息（仅本人）
"""
from i18n import _
import sys, os, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import datetime, timezone
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


# =============================================
# 会话缓存上行（桌面本地缓存 → 服务器）
# =============================================
# 落库为桌面端独有需求，内核既有表无等价物：user_sessions 是**登录会话**
# （token/设备维度），agent_conversations 是 Agent 编排对话（按 master_task_id 归属），
# 二者语义与主键口径均不同，故这里自带两张表；首次调用惰性建表（IF NOT EXISTS，幂等）。
_SYNC_DDL = (
    "CREATE TABLE IF NOT EXISTS user_session_sync ("
    "    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
    "    user_id    BIGINT NOT NULL,"
    "    title      TEXT DEFAULT '',"
    "    synced_at  TEXT DEFAULT '',"
    "    updated_at TEXT DEFAULT ''"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_user_session_sync_user ON user_session_sync(user_id)",
    "CREATE TABLE IF NOT EXISTS user_session_sync_messages ("
    "    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
    "    sync_id  BIGINT NOT NULL,"
    "    seq      BIGINT NOT NULL DEFAULT 0,"
    "    role     TEXT DEFAULT '',"
    "    content  TEXT DEFAULT '',"
    "    ts       TEXT DEFAULT ''"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_user_session_sync_msg ON user_session_sync_messages(sync_id, seq)",
)

_sync_tables_ready = False


def _ensure_sync_tables(conn):
    """惰性建表（幂等）：进程内只执行一次。"""
    global _sync_tables_ready
    if _sync_tables_ready:
        return
    for stmt in _SYNC_DDL:
        conn.execute(stmt)
    _sync_tables_ready = True


# =============================================
# POST /session/sync — upload a locally cached session
# =============================================
@session_bp.route('/sync', methods=['POST'])
def session_sync():
    """全量上行一个本地会话（含消息），返回服务端会话 id 与同步时间。

    入参 {'serverId'?, 'title', 'messages':[{'role','content','ts'}], 'dirty'}；
    带 serverId 且属于本人时覆盖该记录的消息，否则新建记录（增量由前端后续按需扩展）。
    """
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    data = request.get_json(silent=True) or {}
    messages = data.get('messages')
    if not isinstance(messages, list):
        return jsonify({'success': False, 'error': _('messages must be a list')}), 400
    clean = []
    for m in messages:
        if not isinstance(m, dict):
            return jsonify({'success': False, 'error': _('Invalid message')}), 400
        role = str(m.get('role') or '')
        if role not in ('user', 'assistant', 'system'):
            return jsonify({'success': False, 'error': _('Invalid message role')}), 400
        clean.append((role, str(m.get('content') or ''), str(m.get('ts') or '')))
    title = (data.get('title') or '').strip()[:200]
    # 历史本地标记（serverId）可能是非数字串，解析失败即视为未同步过
    try:
        prev_id = int(data['serverId']) if data.get('serverId') not in (None, '') else None
    except (TypeError, ValueError):
        prev_id = None
    synced_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            _ensure_sync_tables(conn)
            sid = None
            if prev_id is not None:
                row = conn.execute(
                    "SELECT id FROM user_session_sync WHERE id=%s AND user_id=%s",
                    (prev_id, uid)
                ).fetchone()
                sid = row['id'] if row else None
            if sid is None:
                sid = conn.execute(
                    "INSERT INTO user_session_sync (user_id, title, synced_at, updated_at) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    (uid, title, synced_at, synced_at)
                ).fetchone()['id']
            else:
                conn.execute(
                    "UPDATE user_session_sync SET title=%s, synced_at=%s, updated_at=%s WHERE id=%s",
                    (title, synced_at, synced_at, sid)
                )
                conn.execute("DELETE FROM user_session_sync_messages WHERE sync_id=%s", (sid,))
            for seq, (role, content, ts) in enumerate(clean):
                conn.execute(
                    "INSERT INTO user_session_sync_messages (sync_id, seq, role, content, ts) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (sid, seq, role, content, ts)
                )
            conn.commit()
        return jsonify({'success': True, 'data': {'session_id': sid, 'synced_at': synced_at}})
    except Exception:
        return jsonify({'success': False, 'error': _('Sync failed')}), 500


# =============================================
# GET /session/<id>/messages — read back a synced session
# =============================================
@session_bp.route('/<int:sid>/messages', methods=['GET'])
def session_sync_messages(sid):
    """读回已上行的会话消息（仅限本人记录）。"""
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    try:
        with get_db() as conn:
            _ensure_sync_tables(conn)
            row = conn.execute(
                "SELECT id, title, synced_at FROM user_session_sync WHERE id=%s AND user_id=%s",
                (sid, uid)
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': _('Session does not exist')}), 404
            rows = conn.execute(
                "SELECT role, content, ts FROM user_session_sync_messages "
                "WHERE sync_id=%s ORDER BY seq",
                (sid,)
            ).fetchall()
        return jsonify({'success': True, 'data': {
            'session_id': row['id'],
            'title': row['title'],
            'synced_at': row['synced_at'],
            'messages': [dict(r) for r in rows],
        }})
    except Exception:
        return jsonify({'success': False, 'error': _('Query failed')}), 500
