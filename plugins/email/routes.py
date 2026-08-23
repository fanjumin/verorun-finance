#!/usr/bin/env python3
"""
Email Plugin Routes — 邮件管理 API 路由
========================================
完全独立，使用插件 PG schema: email + 主库 contact_messages 的 Python 级合并。
"""

from i18n import _
import sys
import os
import io

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify, send_file

email_bp = Blueprint('email', __name__, url_prefix='/admin/email')


def _require_admin():
    """复用主系统的管理员鉴权"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    """复用主系统的操作日志"""
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


# ── GET /admin/email/inbox ──
@email_bp.route('/inbox', methods=['GET'])
def admin_email_inbox():
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import fetch_inbox
    try:
        emails = fetch_inbox(per_page=50)
        return jsonify({'success': True, 'data': emails})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── GET /admin/email/read/<uid> ──
@email_bp.route('/read/<int:uid>', methods=['GET'])
def admin_email_read(uid):
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import read_email
    try:
        email_data = read_email(uid)
        return jsonify({'success': True, 'data': email_data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── POST /admin/email/send ──
@email_bp.route('/send', methods=['POST'])
def admin_email_send():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    to_addr = data.get('to', '').strip()
    subject = data.get('subject', '').strip()
    body = data.get('body', '').strip()
    body_html = data.get('body_html', '')
    attachments = data.get('attachments')
    reply_to_uid = data.get('reply_to_uid')
    if not to_addr or not subject or (not body and not body_html):
        return jsonify({'success': False, 'error': _('Recipient, subject, and content cannot be empty')}), 400
    from plugins.email.services import send_email
    try:
        ok, msg = send_email(to_addr, subject, body or '',
                             body_html=body_html or None,
                             reply_to=reply_to_uid,
                             attachments=attachments)
        if not ok:
            return jsonify({'success': False, 'error': msg}), 400
        _log(admin['user_id'], 'send_email', 'email', '', f'To: {to_addr}, Subject: {subject}')
        return jsonify({'success': True, 'data': {'message': msg}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── GET /admin/email/sent ──
@email_bp.route('/sent', methods=['GET'])
def admin_email_sent():
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import get_sent_emails
    try:
        emails = get_sent_emails(per_page=50)
        return jsonify({'success': True, 'data': emails})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── GET /admin/email/contacts ──
@email_bp.route('/contacts', methods=['GET'])
def admin_email_contacts():
    """合并已发送邮件联系人 + 联系表单联系人（Python 级合并，不依赖 SQL JOIN）"""
    admin, err = _require_admin()
    if err:
        return err
    contacts = {}

    # 1. 从独立 PG schema (email) 读取已发送邮件中的联系人
    from plugins.email.services import get_sent_emails
    sent = get_sent_emails(page=1, per_page=999)
    for item in sent.get('items', []):
        to_addrs = [a.strip() for a in item['to_addr'].split(',') if a.strip()]
        for addr in to_addrs:
            if addr not in contacts:
                contacts[addr] = {'email': addr, 'name': '', 'source': 'sent', 'count': 0}
            contacts[addr]['count'] += 1

    # 2. 从主库 contact_messages 读取联系表单提交的联系人
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT email, name FROM contact_messages WHERE email IS NOT NULL AND email != ''"
            ).fetchall()
            for r in rows:
                addr = r['email'].strip().lower()
                if addr not in contacts:
                    contacts[addr] = {'email': addr, 'name': r['name'] or '', 'source': 'contact', 'count': 0}
                if r['name']:
                    contacts[addr]['name'] = r['name']
    except Exception:
        pass  # contact_messages 表可能不存在，静默跳过

    return jsonify({'success': True, 'data': sorted(contacts.values(), key=lambda c: -c['count'])})


# ── GET /admin/email/settings ──
@email_bp.route('/settings', methods=['GET'])
def admin_email_settings_get():
    """获取邮件服务配置（用于设置页渲染）"""
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import _get_mail_config, CONFIG_DEFS, _MAIL_KEYS
    try:
        cfg = _get_mail_config()
        # 按 _MAIL_KEYS 顺序组织返回，敏感字段掩码显示
        result = {}
        for k in _MAIL_KEYS:
            val = cfg.get(k, '')
            if k == 'smtp_pass' and val:
                val = '********'
            result[k] = val
        return jsonify({'success': True, 'data': {
            'config': result,
            'defs': {k: CONFIG_DEFS.get(k, {}) for k in _MAIL_KEYS},
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── POST /admin/email/settings ──
@email_bp.route('/settings', methods=['POST'])
def admin_email_settings_save():
    """保存邮件服务配置"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    from flask import current_app
    mgr = current_app.extensions.get('plugin_manager')
    if not mgr:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503

    # 只保存 config 中定义的 keys
    cfg = {}
    for k in _MAIL_KEYS:
        if k in data:
            cfg[k] = data[k]

    if not cfg:
        return jsonify({'success': False, 'error': 'No valid config keys provided'}), 400

    # 通过 PluginManager set_config_batch 保存（含类型转换+校验）
    result = mgr.set_config_batch('email', cfg, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': result['errors'], 'data': {'saved': True}})
    return jsonify({'success': True, 'data': {'saved': True}})


# ── GET /admin/email/attachment/<uid>/<filename> ──
@email_bp.route('/attachment/<int:uid>/<path:filename>', methods=['GET'])
def admin_email_attachment(uid, filename):
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import get_attachment
    data, content_type = get_attachment(uid, filename)
    if data is None:
        return jsonify({'success': False, 'error': content_type}), 404
    return send_file(
        io.BytesIO(data),
        mimetype=content_type or 'application/octet-stream',
        as_attachment=True,
        download_name=filename,
    )