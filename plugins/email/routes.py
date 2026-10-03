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
import logging

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify, send_file

from plugins.email.services import _MAIL_KEYS  # 修复 EM-D1: POST handler 使用（原仅 GET handler 内局部导入）
from plugins.email.mail_providers import MAIL_PROVIDERS, get_provider_by_domain  # 服务商预置清单（单一数据源）

email_bp = Blueprint('email', __name__, url_prefix='/admin/email')

logger = logging.getLogger(__name__)


def _list_providers():
    """输出服务商清单（保持旧字段兼容：ssl = smtp_ssl）。"""
    out = []
    for p in MAIL_PROVIDERS:
        item = dict(p)
        item['ssl'] = bool(p.get('smtp_ssl', True))
        out.append(item)
    return out


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
        emails = fetch_inbox(per_page=50, keyword=request.args.get('q', ''))
        return jsonify({'success': True, 'data': emails})
    except Exception:
        logger.exception('[email] inbox failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


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
    except Exception:
        logger.exception('[email] read failed uid=%s', uid)
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


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
    cc = data.get('cc')
    bcc = data.get('bcc')
    if not to_addr or not subject or (not body and not body_html):
        return jsonify({'success': False, 'error': _('Recipient, subject, and content cannot be empty')}), 400
    from plugins.email.services import send_email
    try:
        ok, msg = send_email(to_addr, subject, body or '',
                             body_html=body_html or None,
                             cc=cc, bcc=bcc,
                             reply_to=reply_to_uid,
                             attachments=attachments)
        if not ok:
            return jsonify({'success': False, 'error': msg}), 400
        _log(admin['user_id'], 'send_email', 'email', '', f'To: {to_addr}, Subject: {subject}')
        return jsonify({'success': True, 'data': {'message': msg}})
    except Exception:
        logger.exception('[email] send failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


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
    except Exception:
        logger.exception('[email] sent list failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


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
        # 根据已填账号推导命中的服务商（供前端显示"自动识别"提示）
        suggestion = None
        account = str(result.get('smtp_user') or result.get('smtp_from') or '').strip()
        provider = get_provider_by_domain(account) if account else None
        if provider:
            suggestion = {'id': provider['id'], 'name': provider['name']}
        return jsonify({'success': True, 'data': {
            'config': result,
            'defs': {k: CONFIG_DEFS.get(k, {}) for k in _MAIL_KEYS},
            'providers': _list_providers(),
            'suggestion': suggestion,
        }})
    except Exception:
        logger.exception('[email] settings get failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


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

    # 只保存 config 中定义的 keys；掩码占位值 '********' 视为未修改，保留旧密码
    cfg = {}
    for k in _MAIL_KEYS:
        if k in data:
            if k == 'smtp_pass' and str(data[k]) == '********':
                continue
            cfg[k] = data[k]

    if not cfg:
        return jsonify({'success': False, 'error': 'No valid config keys provided'}), 400

    # EM-1(a)：密码落盘前加密（复用系统 ENCRYPTION_KEY 密钥体系）；
    # crypto 不可用时 encrypt() 内部 fail-open 保持明文，与既有部署一致。
    if cfg.get('smtp_pass'):
        try:
            from plugins.email.crypto import encrypt as _encrypt_secret
            cfg['smtp_pass'] = _encrypt_secret(str(cfg['smtp_pass']))
        except Exception:
            logger.warning('[email] smtp_pass encrypt failed, stored as-is')

    # 保存端域名推导：账号匹配预置服务商且保存内容缺失服务器/端口字段时补全
    # （仅补空字段；用户显式填写的值不会被覆盖）
    account = str(cfg.get('smtp_user') or cfg.get('smtp_from') or '').strip()
    provider = get_provider_by_domain(account) if account else None
    if provider:
        for k, v in (('smtp_host', provider['smtp_host']),
                     ('smtp_port', str(provider['smtp_port'])),
                     ('imap_host', provider['imap_host']),
                     ('imap_port', str(provider['imap_port']))):
            if k not in cfg or not str(cfg.get(k) or '').strip():
                cfg[k] = v

    # 通过 PluginManager set_config_batch 保存（含类型转换+校验）
    result = mgr.set_config_batch('email', cfg, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': result['errors'], 'data': {'saved': True}})
    return jsonify({'success': True, 'data': {'saved': True}})


def _is_blocked_probe_target(host):
    """EM-9 半项：判断探测目标是否落在禁止范围（loopback/私有/链路本地/保留/组播）。

    解析域名后逐个地址判定，任一回落到受保护网段即拒绝；解析失败交由后续
    连接流程自然报错（不在此处伪造成功或失败）。
    """
    import ipaddress
    import socket as _socket
    try:
        infos = _socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0] if info[4] else ''
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


# ── POST /admin/email/test-config ──
@email_bp.route('/test-config', methods=['POST'])
def admin_email_test_config():
    """测试邮件配置：SMTP/IMAP 登录探测（不发送邮件、不修改已保存配置）。

    优先使用请求体中的表单当前值（保存前即可测试）；字段缺省或为掩码
    '********' 时回退到已保存配置，避免把掩码占位当真实密码。
    端口约定：SMTP 465 → 隐式 SSL，其余 → STARTTLS 升级；
              IMAP 993 → 隐式 SSL，其余 → 明文。
    """
    admin, err = _require_admin()
    if err:
        return err

    import smtplib
    import imaplib
    from plugins.email.services import _get_mail_config, get_security_option

    saved = _get_mail_config()
    data = request.get_json(force=True) or {}

    def _pick(key):
        val = data.get(key, '')
        if key in data and str(val) != '********' and val != '':
            return str(val)
        return str(saved.get(key, '') or '')

    smtp_host, smtp_user, smtp_pass = _pick('smtp_host'), _pick('smtp_user'), _pick('smtp_pass')
    imap_host, imap_user, imap_pass = _pick('imap_host'), _pick('smtp_user'), _pick('smtp_pass')
    try:
        smtp_port = int(_pick('smtp_port'))
    except ValueError:
        smtp_port = 0
    try:
        imap_port = int(_pick('imap_port'))
    except ValueError:
        imap_port = 0

    # EM-9 半项：默认禁止对 loopback/私有/保留地址做 SMTP/IMAP 登录探测
    _allow_private = str(get_security_option('allow_private_targets') or '').strip().lower() in (
        '1', 'true', 'yes', 'on')

    result = {'smtp': {'ok': False, 'err': ''}, 'imap': {'ok': False, 'err': ''}}

    # ── SMTP 登录探测 ──
    if not smtp_host or not smtp_port:
        result['smtp']['err'] = _('SMTP 未配置')
    elif not _allow_private and _is_blocked_probe_target(smtp_host):
        result['smtp']['err'] = _("Target address not allowed")
    else:
        try:
            if smtp_port == 465:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
                server.ehlo()
                try:
                    server.starttls()
                except smtplib.SMTPNotSupportedError:
                    pass  # 明文端口（25）无 TLS 能力时允许跳过
                if smtp_user:
                    server.login(smtp_user, smtp_pass)
            server.quit()
            result['smtp']['ok'] = True
        except smtplib.SMTPAuthenticationError as e:
            result['smtp']['err'] = _('SMTP 认证失败: {}').format(e.smtp_code)
        except Exception as e:
            logger.exception('[email] test-config smtp failed')
            result['smtp']['err'] = _("Connection failed") + ": " + type(e).__name__

    # ── IMAP 登录探测 ──
    if not imap_host or not imap_port:
        result['imap']['err'] = _('IMAP 未配置')
    elif not _allow_private and _is_blocked_probe_target(imap_host):
        result['imap']['err'] = _("Target address not allowed")
    else:
        try:
            if imap_port == 993:
                conn = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=12)
            else:
                conn = imaplib.IMAP4(imap_host, imap_port, timeout=12)
            if imap_user:
                conn.login(imap_user, imap_pass)
            conn.logout()
            result['imap']['ok'] = True
        except imaplib.IMAP4.error as e:
            result['imap']['err'] = _('IMAP 认证失败: {}').format(e)
        except Exception as e:
            logger.exception('[email] test-config imap failed')
            result['imap']['err'] = _("Connection failed") + ": " + type(e).__name__

    return jsonify({'success': True, 'data': result})


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


# GET /admin/email/providers
@email_bp.route('/providers', methods=['GET'])
def admin_email_providers():
    admin, err = _require_admin()
    if err:
        return err
    return jsonify({'success': True, 'data': _list_providers()})


# ── GET/POST /admin/email/drafts（草稿箱 v1.7.0） ──
@email_bp.route('/drafts', methods=['GET'])
def admin_email_drafts_list():
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import list_drafts
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    try:
        return jsonify({'success': True, 'data': list_drafts(page=page, per_page=50)})
    except Exception:
        logger.exception('[email] drafts list failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


@email_bp.route('/drafts', methods=['POST'])
def admin_email_drafts_save():
    """新建或更新草稿；body 中带 draft_id 则更新，否则新建。"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    from plugins.email.services import save_draft
    try:
        draft_id = save_draft(
            draft_id=data.get('draft_id'),
            to_addr=str(data.get('to', '') or '').strip(),
            cc_addr=str(data.get('cc', '') or '').strip(),
            bcc_addr=str(data.get('bcc', '') or '').strip(),
            subject=str(data.get('subject', '') or '').strip(),
            body_text=str(data.get('body', '') or ''),
            body_html=str(data.get('body_html', '') or ''),
            attachments=data.get('attachments'))
        if draft_id is None:
            return jsonify({'success': False, 'error': _('草稿不存在或已删除')}), 404
        return jsonify({'success': True, 'data': {'draft_id': draft_id}})
    except Exception:
        logger.exception('[email] draft save failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


@email_bp.route('/drafts/<int:draft_id>', methods=['DELETE'])
def admin_email_drafts_delete(draft_id):
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import delete_draft
    try:
        ok = delete_draft(draft_id)
        if not ok:
            return jsonify({'success': False, 'error': _('草稿不存在')}), 404
        return jsonify({'success': True, 'data': {'deleted': True}})
    except Exception:
        logger.exception('[email] draft delete failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


# ── GET /admin/email/folders（信箱列表 v1.7.0） ──
@email_bp.route('/folders', methods=['GET'])
def admin_email_folders():
    admin, err = _require_admin()
    if err:
        return err
    from plugins.email.services import list_folders
    try:
        result = list_folders()
        if result.get('error'):
            return jsonify({'success': False, 'error': result['error'], 'data': {'folders': []}}), 502
        return jsonify({'success': True, 'data': result})
    except Exception:
        logger.exception('[email] folders failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500


# ── POST /admin/email/batch（信箱批量管理 v1.7.0） ──
@email_bp.route('/batch', methods=['POST'])
def admin_email_batch():
    """批量操作：action ∈ delete | read | unread | move；uids 为邮件 UID 列表，move 需 folder。"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    action = str(data.get('action', '')).strip()
    uids = data.get('uids') or []
    if isinstance(uids, str):
        uids = [u for u in uids.replace('，', ',').split(',') if u.strip()]
    if not uids:
        return jsonify({'success': False, 'error': _('请选择要操作的邮件')}), 400
    from plugins.email.services import delete_emails, mark_read, move_emails
    try:
        if action == 'delete':
            ok, msg = delete_emails(uids)
        elif action == 'read':
            ok, msg = mark_read(uids, read=True)
        elif action == 'unread':
            ok, msg = mark_read(uids, read=False)
        elif action == 'move':
            folder = str(data.get('folder', '')).strip()
            if not folder:
                return jsonify({'success': False, 'error': _('请选择目标文件夹')}), 400
            ok, msg = move_emails(uids, folder)
        else:
            return jsonify({'success': False, 'error': _('不支持的批量操作: {}').format(action)}), 400
        if not ok:
            return jsonify({'success': False, 'error': msg}), 400
        return jsonify({'success': True, 'data': {'message': msg, 'affected': len(uids)}})
    except Exception:
        logger.exception('[email] batch failed')
        return jsonify({'success': False, 'error': _("Internal error, please check server logs")}), 500
