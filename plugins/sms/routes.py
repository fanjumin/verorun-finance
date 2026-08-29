#!/usr/bin/env python3
"""
SMS Plugin Routes — SMS 模板管理 + 发送 API
============================================
完全独立，使用插件 PG schema `sms` + 主库 system_config 只读。
"""
from i18n import _
import sys
import os
import psycopg2

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify

from .models import get_sms_db

sms_bp = Blueprint('sms', __name__, url_prefix='/admin/sms')


def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


# ── GET /admin/sms/templates ──
@sms_bp.route('/templates', methods=['GET'])
def sms_templates_list():
    """获取所有短信模板（按分类分组）"""
    admin, err = _require_admin()
    if err:
        return err
    with get_sms_db() as conn:
        rows = conn.execute(
            'SELECT id, category, name, template_code, note, sort_order FROM sms_templates ORDER BY sort_order'
        ).fetchall()
    templates = [dict(r) for r in rows]
    categories = {
        'captcha': {'title': _('verification_code'), 'items': []},
        'notice':  {'title': _('sms_notification'), 'items': []},
        'promo':   {'title': _('sms_promotion'), 'items': []},
    }
    for t in templates:
        cat = t.get('category', 'promo')
        if cat in categories:
            categories[cat]['items'].append(t)
    return jsonify({'success': True, 'data': {'categories': categories, 'all': templates}})


# ── POST /admin/sms/templates ──
@sms_bp.route('/templates', methods=['POST'])
def sms_template_create():
    """创建短信模板"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    category = data.get('category', '').strip()
    name = data.get('name', '').strip()
    template_code = data.get('template_code', '').strip()
    note = data.get('note', '').strip()
    if not category or not name or not template_code:
        return jsonify({'success': False, 'error': _('Category, name, and template code cannot be empty')}), 400
    if category not in ('captcha', 'notice', 'promo'):
        return jsonify({'success': False, 'error': _('Invalid category, must be captcha/notice/promo')}), 400
    with get_sms_db() as conn:
        row = conn.execute('SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM sms_templates').fetchone()
        sort_order = row['n']
        try:
            cur = conn.execute(
                'INSERT INTO sms_templates (category, name, template_code, note, sort_order) VALUES (?,?,?,?,?) RETURNING id',
                (category, name, template_code, note, sort_order)
            )
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()
            return jsonify({'success': False, 'error': _('Template already exists in this category')}), 409
        tid = cur.fetchone()['id']
        _log(admin['user_id'], 'create_sms_template', 'sms', str(tid), f'{category}/{name}')
        return jsonify({'success': True, 'data': {'id': tid}})


# ── PUT /admin/sms/templates/<tid> ──
@sms_bp.route('/templates/<int:tid>', methods=['PUT'])
def sms_template_update(tid):
    """更新短信模板"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    fields = []
    params = []
    for key in ('name', 'template_code', 'note', 'category', 'sort_order'):
        if key in data:
            fields.append(f'{key}=?')
            params.append(data[key])
    if not fields:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    params.append(tid)
    with get_sms_db() as conn:
        conn.execute(
            f'UPDATE sms_templates SET {", ".join(fields)}, updated_at=NOW() WHERE id=?',
            params
        )
        conn.commit()
        _log(admin['user_id'], 'update_sms_template', 'sms', str(tid), '')
        return jsonify({'success': True})


# ── DELETE /admin/sms/templates/<tid> ──
@sms_bp.route('/templates/<int:tid>', methods=['DELETE'])
def sms_template_delete(tid):
    """删除短信模板"""
    admin, err = _require_admin()
    if err:
        return err
    with get_sms_db() as conn:
        conn.execute('DELETE FROM sms_templates WHERE id=?', (tid,))
        conn.commit()
        _log(admin['user_id'], 'delete_sms_template', 'sms', str(tid), '')
        return jsonify({'success': True})


# ── GET /admin/sms/logs ──
@sms_bp.route('/logs', methods=['GET'])
def sms_logs_list():
    """获取短信发送日志"""
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    offset = (page - 1) * per_page
    with get_sms_db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM sms_logs').fetchone()['count']
        rows = conn.execute(
            'SELECT * FROM sms_logs ORDER BY created_at DESC LIMIT ? OFFSET ?',
            (per_page, offset)
        ).fetchall()
    return jsonify({
        'success': True,
        'data': {
            'items': [dict(r) for r in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
        }
    })


# ── POST /admin/sms/test-send ──
@sms_bp.route('/test-send', methods=['POST'])
def sms_test_send():
    """测试短信发送（支持按手机号选择提供商）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    phone = data.get('phone', '').strip()
    code = data.get('code', '123456')
    country_code = data.get('country_code', '')
    purpose = data.get('purpose', 'test')
    if not phone:
        return jsonify({'success': False, 'error': _('Phone number cannot be empty')}), 400

    # 如果传了 country_code 且手机号无 + 前缀，自动拼接
    if country_code and not phone.startswith('+'):
        phone = country_code + phone.lstrip('+')

    from plugins.sms.services import send_sms
    try:
        result = send_sms(phone, code, purpose=purpose)
    except Exception as e:
        result = {'success': False, 'error': str(e)}
    if result.get('success'):
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': result.get('error', _('Send Failed'))}), 400


# ─── PluginManager 标准化配置 ─────────────────────────────────────────

_SMS_CONFIG_KEYS = ['aliyun_sms_sign_name', 'aliyun_sms_access_key', 'aliyun_sms_secret']

_SMS_DEFAULTS = {
    'aliyun_sms_sign_name': '',
    'aliyun_sms_access_key': '',
    'aliyun_sms_secret': '',
}


def _get_sms_pm():
    import flask
    try:
        return flask.current_app.extensions.get('plugin_manager')
    except Exception:
        return None


@sms_bp.route('/settings', methods=['GET'])
def sms_settings_get():
    admin, err = _require_admin()
    if err:
        return err
    pm = _get_sms_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    cfg = pm.get_config('sms') or {}
    result = {}
    for k in _SMS_CONFIG_KEYS:
        v = cfg.get(k)
        if v is not None:
            result[k] = v
        else:
            result[k] = _SMS_DEFAULTS.get(k, '')
    # 敏感字段脱敏，避免明文返回 AccessKey Secret
    if result.get('aliyun_sms_secret'):
        result['aliyun_sms_secret'] = '******'
    return jsonify({'success': True, 'data': result})


@sms_bp.route('/settings', methods=['POST'])
def sms_settings_save():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    pm = _get_sms_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    filtered = {}
    for k, v in data.items():
        if k in _SMS_CONFIG_KEYS:
            filtered[k] = str(v) if v is not None else ''
    if not filtered:
        return jsonify({'success': False, 'error': 'No valid config keys'}), 400
    result = pm.set_config_batch('sms', filtered, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': str(result['errors'])})
    return jsonify({'success': True})


# ── GET /admin/sms/countries ──
@sms_bp.route('/countries', methods=['GET'])
def sms_countries_list():
    """获取支持的国家列表（含国旗 emoji、区号、中文名、英文名）"""
    from .countries import COUNTRIES
    return jsonify({'success': True, 'data': COUNTRIES})
