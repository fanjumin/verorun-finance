#!/usr/bin/env python3
"""Agent Routes — user-owned 智能体 management.
   
   Human User → owns → User Agent → has → API Keys with scopes.
   
   New architecture (2026-05-10):
   - user_agents table: user's AI agent identity (separate from human user)
   - agent_api_keys table: per-agent API keys with scope control
   - agent_logs table: agent action audit trail
"""
from i18n import _
import sys, os, secrets, hashlib, json as _json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from flask import Blueprint, request, jsonify
from models import get_db, TIERS
from services.jwt_service import validate_token

agent_bp = Blueprint('agent', __name__, url_prefix='/agent')


def _require_auth():
    """Extract and validate JWT from Authorization header"""
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    payload = validate_token(token)
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Not logged in or token expired')}), 401)
    return payload, None


def _log(agent_id, user_id, action, detail=''):
    ip = request.remote_addr or ''
    with get_db() as conn:
        conn.execute(
            'INSERT INTO agent_logs (agent_id, user_id, action, detail, ip_address) VALUES (%s,%s,%s,%s,%s)',
            (agent_id, user_id, action, detail, ip)
        )
        conn.commit()


# =============================================
# GET /agent/list — list current user's agents
# =============================================
@agent_bp.route('/list', methods=['GET'])
def agent_list():
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ua.id, ua.agent_name, ua.agent_type, ua.avatar_url, ua.status, "
            "       ua.default_scopes, ua.metadata, ua.last_active_at, ua.created_at, "
            "       (SELECT COUNT(*) FROM agent_api_keys WHERE agent_id=ua.id AND status='active') as active_keys "
            "FROM user_agents ua WHERE ua.user_id=%s ORDER BY ua.created_at DESC",
            (uid,)
        ).fetchall()
    agents = []
    for r in rows:
        d = dict(r)
        try:
            d['default_scopes'] = _json.loads(d['default_scopes'] or '[]')
        except Exception:
            d['default_scopes'] = []
        try:
            d['metadata'] = _json.loads(d['metadata'] or '{}')
        except Exception:
            d['metadata'] = {}
        agents.append(d)
    return jsonify({'success': True, 'data': agents})


# =============================================
# POST /agent/create — create a new agent
# =============================================
@agent_bp.route('/create', methods=['POST'])
def agent_create():
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    data = request.get_json(force=True) or {}
    agent_name = data.get('agent_name', '').strip()
    agent_type = data.get('agent_type', 'personal').strip()
    default_scopes = data.get('default_scopes', ['stock:read', 'market:alert'])
    
    if not agent_name:
        return jsonify({'success': False, 'error': _('Agent name cannot be empty')}), 400
    
    if agent_type not in ('personal', 'community', 'trading'):
        agent_type = 'personal'
    
    with get_db() as conn:
        # Check agent limit per tier
        user = conn.execute("SELECT u.id, COALESCE(aa.tier,'free') as tier FROM users u LEFT JOIN app_authorizations aa ON u.id=aa.user_id AND aa.app_name='main' WHERE u.id=%s", (uid,)).fetchone()
        tier = user['tier'] if user else 'free'
        max_agents = TIERS.get(tier, {}).get('max_agents', 1)
        existing_count = conn.execute("SELECT COUNT(*) as c FROM user_agents WHERE user_id=%s", (uid,)).fetchone()['c']
        if existing_count >= max_agents:
            return jsonify({'success': False, 'error': f'Your {tier} plan allows up to {max_agents} Agents, and you currently have {existing_count}'}), 400
        
        # Check duplicate name for this user
        existing = conn.execute(
            "SELECT id FROM user_agents WHERE user_id=%s AND agent_name=%s",
            (uid, agent_name)
        ).fetchone()
        if existing:
            return jsonify({'success': False, 'error': f'An Agent named "{agent_name}" already exists'}), 400
        
        scopes_str = _json.dumps(default_scopes if isinstance(default_scopes, list) else ['stock:read', 'market:alert'])
        metadata_str = _json.dumps(data.get('metadata', {}))
        
        aid = conn.execute(
            "INSERT INTO user_agents (user_id, agent_name, agent_type, avatar_url, default_scopes, metadata) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (uid, agent_name, agent_type, f'/avatar/gen/{agent_name}', scopes_str, metadata_str)
        ).fetchone()['id']
        conn.commit()
    
    _log(aid, uid, 'create', f'Agent "{agent_name}" created')
    
    return jsonify({'success': True, 'data': {
        'id': aid,
        'agent_name': agent_name,
        'agent_type': agent_type,
        'default_scopes': default_scopes,
        'status': 'active',
        'created_at': 'now',
        'message': _('Agent created successfully! You can now generate an API Key.'),
    }})


# =============================================
# GET /agent/<id> — agent detail
# =============================================
@agent_bp.route('/<int:aid>', methods=['GET'])
def agent_detail(aid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, user_id, agent_name, agent_type, avatar_url, status, "
            "       default_scopes, metadata, last_active_ip, last_active_at, "
            "       created_at, updated_at "
            "FROM user_agents WHERE id=%s AND user_id=%s",
            (aid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        
        d = dict(row)
        try:
            d['default_scopes'] = _json.loads(d['default_scopes'] or '[]')
        except Exception:
            d['default_scopes'] = []
        try:
            d['metadata'] = _json.loads(d['metadata'] or '{}')
        except Exception:
            d['metadata'] = {}
        
        # Keys
        keys = conn.execute(
            "SELECT id, key_prefix, name, scopes, status, expire_at, "
            "       last_used_at, rotated_at, calls_today, calls_total, created_at "
            "FROM agent_api_keys WHERE agent_id=%s ORDER BY created_at DESC",
            (aid,)
        ).fetchall()
        d['api_keys'] = [dict(k) for k in keys]
        for k in d['api_keys']:
            try:
                k['scopes'] = _json.loads(k['scopes'] or '[]')
            except Exception:
                k['scopes'] = []
    
    return jsonify({'success': True, 'data': d})


# =============================================
# PUT /agent/<id> — update agent
# =============================================
@agent_bp.route('/<int:aid>', methods=['PUT'])
def agent_update(aid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    data = request.get_json(force=True) or {}
    
    fields = []
    params = []
    for key in ('agent_name', 'agent_type', 'avatar_url', 'status', 'default_scopes', 'metadata'):
        if key in data:
            val = data[key]
            if key in ('default_scopes', 'metadata') and isinstance(val, (list, dict)):
                val = _json.dumps(val)
            fields.append(f'{key}=%s')
            params.append(val)
    
    if not fields:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    
    fields.append("updated_at=NOW()")
    params.extend([aid, uid])
    
    with get_db() as conn:
        # Verify ownership
        row = conn.execute("SELECT id FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        conn.execute(
            f'UPDATE user_agents SET {", ".join(fields)} WHERE id=%s AND user_id=%s',
            params
        )
        conn.commit()
    
    _log(aid, uid, 'update', f'Updated fields: {", ".join(data.keys())}')
    return jsonify({'success': True, 'message': _('Agent has been updated')})


# =============================================
# DELETE /agent/<id> — delete agent (and revoke all keys)
# =============================================
@agent_bp.route('/<int:aid>', methods=['DELETE'])
def agent_delete(aid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    
    with get_db() as conn:
        row = conn.execute("SELECT id, agent_name FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        
        agent_name = row['agent_name']
        # Delete in FK order: api_keys → logs → agent
        conn.execute("DELETE FROM agent_api_keys WHERE agent_id=%s", (aid,))
        conn.execute("DELETE FROM agent_logs WHERE agent_id=%s", (aid,))
        conn.execute("DELETE FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid))
        conn.commit()
    
    # Log without FK reference (use user_id only)
    ip = request.remote_addr or ''
    with get_db() as conn:
        conn.execute(
            'INSERT INTO agent_logs (agent_id, user_id, action, detail, ip_address) VALUES (NULL,%s,%s,%s,%s)',
            (uid, 'delete', f'Agent "{agent_name}" deleted (id={aid})', ip)
        )
        conn.commit()
    
    return jsonify({'success': True, 'message': f'Agent "{agent_name}" has been deleted'})


# =============================================
# GET /agent/<id>/keys — list agent's API keys
# =============================================
@agent_bp.route('/<int:aid>/keys', methods=['GET'])
def agent_keys_list(aid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        row = conn.execute("SELECT id FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        
        keys = conn.execute(
            "SELECT id, key_prefix, name, scopes, status, expire_at, "
            "       last_used_at, rotated_at, calls_today, calls_total, created_at "
            "FROM agent_api_keys WHERE agent_id=%s ORDER BY created_at DESC",
            (aid,)
        ).fetchall()
    
    result = []
    for k in keys:
        d = dict(k)
        try:
            d['scopes'] = _json.loads(d['scopes'] or '[]')
        except Exception:
            d['scopes'] = []
        result.append(d)
    
    return jsonify({'success': True, 'data': result})


# =============================================
# POST /agent/<id>/keys/create — generate new API key (shown once!)
# =============================================
@agent_bp.route('/<int:aid>/keys/create', methods=['POST'])
def agent_key_create(aid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    scopes = data.get('scopes', None)
    expire_days = data.get('expire_days', 365)
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, agent_name FROM user_agents WHERE id=%s AND user_id=%s",
            (aid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
    
    # Generate key
    raw_key = 'ek-' + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12] + '...' + raw_key[-4:]
    
    # Determine scopes: explicit override, or inherit from agent
    if scopes is None:
        scopes_str = ''  # empty = inherit from agent
    else:
        scopes_str = _json.dumps(scopes if isinstance(scopes, list) else [])
    
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_api_keys (agent_id, user_id, key_hash, key_prefix, name, scopes, expire_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,NOW() + (%s * INTERVAL '1 second'))",
            (aid, uid, key_hash, key_prefix, name, scopes_str, expire_days * 86400)
        )
        conn.commit()
        kid = conn.execute('SELECT lastval()').fetchone()['lastval']
    
    _log(aid, uid, 'create_key', f'Key "{name or "unnamed"}" created (expires in {expire_days}d)')
    
    return jsonify({'success': True, 'data': {
        'id': kid,
        'key': raw_key,
        'key_prefix': key_prefix,
        'name': name,
        'expire_days': expire_days,
        'warning': '⚠️ 密钥只显示一次！关闭后将无法再次查看完整密钥。请立即复制保存。',
    }})


# =============================================
# DELETE /agent/<id>/keys/<kid> — revoke API key
# =============================================
@agent_bp.route('/<int:aid>/keys/<int:kid>', methods=['DELETE'])
def agent_key_revoke(aid, kid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        # Verify agent belongs to user
        row = conn.execute("SELECT id FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        # Verify key belongs to agent
        key = conn.execute("SELECT id, name FROM agent_api_keys WHERE id=%s AND agent_id=%s", (kid, aid)).fetchone()
        if not key:
            return jsonify({'success': False, 'error': _('Key does not exist')}), 404
        conn.execute("UPDATE agent_api_keys SET status='revoked' WHERE id=%s", (kid,))
        conn.commit()
    
    _log(aid, uid, 'revoke_key', f'Key "{key["name"] or kid}" revoked')
    return jsonify({'success': True, 'message': _('Key has been canceled')})


# =============================================
# POST /agent/<id>/keys/<kid>/rotate — rotate API key
# =============================================
@agent_bp.route('/<int:aid>/keys/<int:kid>/rotate', methods=['POST'])
def agent_key_rotate(aid, kid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    
    with get_db() as conn:
        row = conn.execute("SELECT id FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        
        key = conn.execute(
            "SELECT id, name, key_prefix FROM agent_api_keys WHERE id=%s AND agent_id=%s AND status='active'",
            (kid, aid)
        ).fetchone()
        if not key:
            return jsonify({'success': False, 'error': _('Key does not exist or has expired')}), 404
    
    # Generate new key
    raw_key = 'ek-' + secrets.token_hex(24)
    new_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    new_prefix = raw_key[:12] + '...' + raw_key[-4:]
    
    with get_db() as conn:
        # Mark old key as revoked and record rotation
        conn.execute(
            "UPDATE agent_api_keys SET status='revoked', rotated_at=NOW() WHERE id=%s",
            (kid,)
        )
        # Create new key with rotation link
        conn.execute(
            "INSERT INTO agent_api_keys (agent_id, user_id, key_hash, key_prefix, name, "
            "  scopes, status, expire_at, rotated_from_key_id) "
            "SELECT %s, %s, %s, %s, name, scopes, 'active', expire_at, %s "
            "FROM agent_api_keys WHERE id=%s",
            (aid, uid, new_hash, new_prefix, kid, kid)
        )
        conn.commit()
        new_kid = conn.execute('SELECT lastval()').fetchone()['lastval']
    
    _log(aid, uid, 'rotate_key', f'Key {kid} rotated → {new_kid}')
    
    return jsonify({'success': True, 'data': {
        'id': new_kid,
        'key': raw_key,
        'key_prefix': new_prefix,
        'rotated_from': kid,
        'warning': _('⚠️ New key is shown only once! Old key has been automatically revoked.'),
    }})


# =============================================
# GET /agent/<id>/stats — agent usage statistics
# =============================================
@agent_bp.route('/<int:aid>/stats', methods=['GET'])
def agent_stats(aid):
    payload, err = _require_auth()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        row = conn.execute("SELECT id, agent_name FROM user_agents WHERE id=%s AND user_id=%s", (aid, uid)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Agent does not exist or does not belong to the current user')}), 404
        
        # Total calls
        total_calls = conn.execute(
            "SELECT COALESCE(SUM(calls_total),0) as c FROM agent_api_keys WHERE agent_id=%s",
            (aid,)
        ).fetchone()
        today_calls = conn.execute(
            "SELECT COALESCE(SUM(calls_today),0) as c FROM agent_api_keys WHERE agent_id=%s AND last_reset=CURRENT_DATE",
            (aid,)
        ).fetchone()
        active_keys = conn.execute(
            "SELECT COUNT(*) as c FROM agent_api_keys WHERE agent_id=%s AND status='active'",
            (aid,)
        ).fetchone()
        revoked_keys = conn.execute(
            "SELECT COUNT(*) as c FROM agent_api_keys WHERE agent_id=%s AND status='revoked'",
            (aid,)
        ).fetchone()
        log_count = conn.execute(
            "SELECT COUNT(*) as c FROM agent_logs WHERE agent_id=%s",
            (aid,)
        ).fetchone()
    
    return jsonify({'success': True, 'data': {
        'agent_id': aid,
        'agent_name': row['agent_name'],
        'total_api_calls': total_calls['c'],
        'today_api_calls': today_calls['c'],
        'active_keys': active_keys['c'],
        'revoked_keys': revoked_keys['c'],
        'total_log_entries': log_count['c'],
    }})
