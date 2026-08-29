#!/usr/bin/env python3
"""Enterprise Verification Plugin — 管理端 API 路由"""
import sys, os, json

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify

ev_admin_bp = Blueprint('enterprise_verify_admin', __name__, url_prefix='/admin/enterprise-verifications')


def _require_admin():
    """复用主系统的管理员鉴权"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    """复用主系统的操作日志"""
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


def _get_main_db():
    """获取主系统数据库连接"""
    from models import get_db
    return get_db()


def _get_ev_db():
    """获取插件数据库连接"""
    from plugins.enterprise_verify.models import get_ev_db
    return get_ev_db()


# ── GET /admin/enterprise-verifications ──
@ev_admin_bp.route('/', methods=['GET'])
def enterprise_verification_list():
    admin, err = _require_admin()
    if err:
        return err

    status = request.args.get("status", "pending")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 20, type=int)
    offset = (page - 1) * limit

    with _get_ev_db() as ev_conn:
        total = ev_conn.execute(
            "SELECT COUNT(*) as c FROM enterprise_verifications WHERE status=?",
            (status,)
        ).fetchone()['c']

        # 1) 插件库查认证记录（不跨库 JOIN，只取必要列）
        ev_rows = ev_conn.execute("""
            SELECT id, user_id, enterprise_name, tax_id, license_url, status, review_notes, reviewed_by, reviewed_at, created_at
            FROM enterprise_verifications
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (status, limit, offset)).fetchall()
        verifications = [dict(r) for r in ev_rows]

    # 2) 主库批量补充用户信息（display_name/phone/email）
    user_ids = list({v['user_id'] for v in verifications if v.get('user_id')})
    user_map = {}
    if user_ids:
        placeholders = ','.join(['%s'] * len(user_ids))
        with _get_main_db() as conn:
            urows = conn.execute(
                f"SELECT id, display_name, phone, email FROM users WHERE id IN ({placeholders})",
                user_ids
            ).fetchall()
            user_map = {u['id']: dict(u) for u in urows}

    # 3) Python 内合并
    for v in verifications:
        u = user_map.get(v.get('user_id'), {})
        v['display_name'] = u.get('display_name')
        v['phone'] = u.get('phone')
        v['email'] = u.get('email')

    return jsonify({
        "success": True,
        "data": {
            "total": total,
            "verifications": verifications,
        }
    })


# ── POST /admin/enterprise-verifications/<id>/approve ──
@ev_admin_bp.route('/<int:ev_id>/approve', methods=['POST'])
def enterprise_verify_approve(ev_id):
    admin, err = _require_admin()
    if err:
        return err

    data = request.get_json(force=True) or {}
    notes = (data.get('notes') or '').strip()

    with _get_ev_db() as ev_conn:
        ev = ev_conn.execute(
            "SELECT * FROM enterprise_verifications WHERE id=?", (ev_id,)
        ).fetchone()
        if not ev:
            return jsonify({'success': False, 'error': 'Verification record not found'}), 404

        ev_conn.execute(
            "UPDATE enterprise_verifications SET status='approved', review_notes=%s, reviewed_by=%s, reviewed_at=NOW(), updated_at=NOW() WHERE id=%s",
            (notes, admin['user_id'], ev_id)
        )
        ev_conn.commit()

    # 更新主系统 users 表
    with _get_main_db() as conn:
        conn.execute(
            "UPDATE users SET enterprise_name=%s, enterprise_tax_id=%s, enterprise_verified=1, enterprise_verified_at=NOW() WHERE id=%s",
            (ev['enterprise_name'], ev['tax_id'], ev['user_id'])
        )
        conn.commit()

    _log(admin['user_id'], 'approve_enterprise_verify', detail=f'id={ev_id} user={ev["user_id"]}')
    return jsonify({'success': True, 'message': 'Enterprise Verified'})


# ── POST /admin/enterprise-verifications/<id>/reject ──
@ev_admin_bp.route('/<int:ev_id>/reject', methods=['POST'])
def enterprise_verify_reject(ev_id):
    admin, err = _require_admin()
    if err:
        return err

    data = request.get_json(force=True) or {}
    notes = (data.get('notes') or '').strip()
    if not notes:
        return jsonify({'success': False, 'error': 'Please enter a reason for rejection'}), 400

    with _get_ev_db() as ev_conn:
        ev = ev_conn.execute(
            "SELECT * FROM enterprise_verifications WHERE id=?", (ev_id,)
        ).fetchone()
        if not ev:
            return jsonify({'success': False, 'error': 'Verification record not found'}), 404

        ev_conn.execute(
            "UPDATE enterprise_verifications SET status='rejected', review_notes=%s, reviewed_by=%s, reviewed_at=NOW(), updated_at=NOW() WHERE id=%s",
            (notes, admin['user_id'], ev_id)
        )
        ev_conn.commit()

    _log(admin['user_id'], 'reject_enterprise_verify', detail=f'id={ev_id} user={ev["user_id"]}')
    return jsonify({'success': True, 'message': 'Enterprise Verification Rejected'})


# ─── PluginManager 标准化配置 ─────────────────────────────────────────

_EV_CONFIG_KEYS = ['siliconflow_api_key', 'auto_approve', 'max_retry']

_EV_DEFAULTS = {
    'siliconflow_api_key': '',
    'auto_approve': False,
    'max_retry': 3,
}


def _get_ev_pm():
    import flask
    try:
        return flask.current_app.extensions.get('plugin_manager')
    except Exception:
        return None


@ev_admin_bp.route('/settings', methods=['GET'])
def ev_settings_get():
    admin, err = _require_admin()
    if err:
        return err
    pm = _get_ev_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    cfg = pm.get_config('enterprise_verify') or {}
    result = {}
    for k in _EV_CONFIG_KEYS:
        v = cfg.get(k)
        if v is not None:
            result[k] = v
        else:
            result[k] = _EV_DEFAULTS.get(k)
    return jsonify({'success': True, 'data': result})


@ev_admin_bp.route('/settings', methods=['POST'])
def ev_settings_save():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    pm = _get_ev_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    filtered = {}
    for k in _EV_CONFIG_KEYS:
        if k in data:
            v = data[k]
            if k == 'max_retry':
                try:
                    filtered[k] = int(v)
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'error': f'{k} must be integer'}), 400
            elif k == 'auto_approve':
                if isinstance(v, str):
                    filtered[k] = v.lower() in ('1', 'true', 'yes')
                else:
                    filtered[k] = bool(v)
            else:
                filtered[k] = str(v) if v is not None else ''
    if not filtered:
        return jsonify({'success': False, 'error': 'No valid config keys'}), 400
    result = pm.set_config_batch('enterprise_verify', filtered, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': str(result['errors'])})
    return jsonify({'success': True})