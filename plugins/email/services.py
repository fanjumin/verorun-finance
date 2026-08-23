#!/usr/bin/env python3
"""
Email Service — 统一邮件服务（SMTP 发信 + IMAP 收信 + 附件）
==============================================================
合并原 email_client.py 和 mail_service.py，提供统一的邮件接口。
完全独立于主库，使用独立 PG schema: email + 环境变量配置。

配置来源优先级：环境变量 → plugin.json 默认值

环境变量              | 说明                    | 默认值
---------------------|-------------------------|--------------------------
SMTP_HOST            | SMTP 服务器              | smtp.qiye.aliyun.com
SMTP_PORT            | SMTP 端口                | 465
SMTP_USER            | SMTP 账号                | （必填）
SMTP_PASS            | SMTP 密码                | （必填）
SMTP_FROM            | 发件人地址              | 同 SMTP_USER
IMAP_HOST            | IMAP 服务器             | imap.qiye.aliyun.com
IMAP_PORT            | IMAP 端口               | 993
"""

import os
import re
import email
import quopri
import base64
import json
import logging
import smtplib
import ssl
import imaplib

from i18n import _
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.application import MIMEApplication
from email.utils import formataddr, parsedate_to_datetime

logger = logging.getLogger(__name__)

_MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB

# ── Config keys ──
_MAIL_KEYS = ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'smtp_from', 'imap_host', 'imap_port']
_DEFAULTS = {
    'smtp_host': 'smtp.qiye.aliyun.com', 'smtp_port': '465',
    'smtp_user': '', 'smtp_pass': '', 'smtp_from': '',
    'imap_host': 'imap.qiye.aliyun.com', 'imap_port': '993',
}
_ENV_MAP = {
    'smtp_host': 'SMTP_HOST', 'smtp_port': 'SMTP_PORT',
    'smtp_user': 'SMTP_USER', 'smtp_pass': 'SMTP_PASS',
    'smtp_from': 'SMTP_FROM', 'imap_host': 'IMAP_HOST', 'imap_port': 'IMAP_PORT',
}

CONFIG_DEFS = {
    'smtp_host':  {'label': _('SMTP 服务器'),    'default': 'smtp.qiye.aliyun.com', 'sensitive': False},
    'smtp_port':  {'label': _('SMTP 端口'),      'default': '465',                  'sensitive': False},
    'smtp_user':  {'label': _('SMTP 账号'),      'default': '',                     'sensitive': False},
    'smtp_pass':  {'label': _('SMTP 密码'),      'default': '',                     'sensitive': True},
    'smtp_from':  {'label': _('发件人地址'),      'default': '',                     'sensitive': False},
    'imap_host':  {'label': _('IMAP 服务器'),     'default': 'imap.qiye.aliyun.com','sensitive': False},
    'imap_port':  {'label': _('IMAP 端口'),       'default': '993',                  'sensitive': False},
}


# ─── Config helpers ────────────────────────────────────────────────────────

def _get_plugin_manager_config():
    """尝试从 PluginManager 读取邮件配置（优先级次于 env var，高于 system_config）"""
    try:
        from flask import current_app
        mgr = current_app.extensions.get('plugin_manager')
        if mgr:
            return mgr.get_config('email') or {}
    except Exception:
        pass
    return {}


def _get_mail_config():
    """Merge env → plugin_manager → system_config → defaults for all mail config keys."""
    cfg = {}

    # 1. 尝试从 PluginManager 读取（新建/编辑设置页的配置）
    pm_config = _get_plugin_manager_config()

    # 2. 尝试从主库 system_config 读取（兼容旧配置）
    db_config = {}
    try:
        from models import get_db
        keys = list(_MAIL_KEYS)
        placeholders = ','.join('?' for _ in keys)
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM system_config WHERE key IN ({placeholders})", keys
            ).fetchall()
            for r in rows:
                db_config[r['key']] = r['value']
    except Exception:
        pass

    # 3. 优先级：环境变量 > plugin_manager > system_config > 默认值
    for k in _MAIL_KEYS:
        cfg[k] = (os.environ.get(_ENV_MAP[k], '')
                  or str(pm_config.get(k, '') or '')
                  or db_config.get(k, '')
                  or _DEFAULTS[k])
    if not cfg['smtp_from']:
        cfg['smtp_from'] = cfg['smtp_user']
    if not cfg['smtp_user']:
        cfg['smtp_user'] = cfg['smtp_from']
    try:
        cfg['smtp_port'] = int(cfg['smtp_port'])
    except (ValueError, TypeError):
        cfg['smtp_port'] = 465
    try:
        cfg['imap_port'] = int(cfg['imap_port'])
    except (ValueError, TypeError):
        cfg['imap_port'] = 993
    return cfg


def get_smtp_config():
    """获取 SMTP/IMAP 配置（兼容旧接口）"""
    return _get_mail_config()


# ─── IMAP Helpers ──────────────────────────────────────────────────────────

def _connect_imap():
    cfg = _get_mail_config()
    imap = imaplib.IMAP4_SSL(cfg['imap_host'], cfg['imap_port'])
    imap.login(cfg['smtp_user'], cfg['smtp_pass'])
    imap.select("INBOX")
    return imap


def _decode_mime_header(val):
    if not val:
        return ""
    parts = decode_header(val)
    result = []
    for data, charset in parts:
        if isinstance(data, bytes):
            try:
                result.append(data.decode(charset or "utf-8", errors="replace"))
            except:
                result.append(data.decode("utf-8", errors="replace"))
        else:
            result.append(str(data))
    return "".join(result)


def _decode_body(payload, encoding=None):
    if encoding:
        try:
            if encoding.lower() in ("base64", "b"):
                payload = base64.b64decode(payload)
            elif encoding.lower() in ("quoted-printable", "q"):
                payload = quopri.decodestring(payload)
        except:
            pass
    if isinstance(payload, bytes):
        for cs in ("utf-8", "gbk", "gb2312", "latin-1"):
            try:
                return payload.decode(cs)
            except:
                continue
        return payload.decode("utf-8", errors="replace")
    return payload


def _get_text_from_part(part):
    ct = part.get_content_type()
    encoding = part.get("Content-Transfer-Encoding", "")
    payload = part.get_payload(decode=True)
    if ct == "text/plain":
        return _decode_body(payload, encoding)
    elif ct == "text/html":
        return _decode_body(payload, encoding)
    return None


def _get_email_body(msg):
    """Return (plain_text, html_text) tuple."""
    plain_text, html_text = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if part.is_multipart():
                continue
            encoding = part.get("Content-Transfer-Encoding", "")
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if ct == "text/plain":
                plain_text = _decode_body(payload, encoding)
            elif ct == "text/html":
                html_text = _decode_body(payload, encoding)
    else:
        ct = msg.get_content_type()
        encoding = msg.get("Content-Transfer-Encoding", "")
        payload = msg.get_payload(decode=True)
        if ct == "text/plain":
            plain_text = _decode_body(payload, encoding)
        elif ct == "text/html":
            html_text = _decode_body(payload, encoding)
    return plain_text or _("(无文本内容)"), html_text


def _get_attachments_from_msg(msg):
    """Extract attachment info from email message."""
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get_content_maintype() == 'text':
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_mime_header(filename)
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > _MAX_ATTACHMENT_SIZE:
            attachments.append({
                "filename": filename,
                "size": len(payload),
                "content_type": part.get_content_type(),
                "too_large": True,
            })
            continue
        attachments.append({
            "filename": filename,
            "size": len(payload),
            "content_type": part.get_content_type(),
            "data": base64.b64encode(payload).decode(),
            "too_large": False,
        })
    return attachments


# ─── IMAP Public API ───────────────────────────────────────────────────────

def fetch_inbox(page=1, per_page=20):
    try:
        imap = _connect_imap()
    except Exception as e:
        return {"error": _("IMAP 连接失败: {}").format(e), "items": [], "total": 0}
    try:
        status, data = imap.search(None, "ALL")
        if status != "OK":
            return {"error": _("无法搜索收件箱"), "items": [], "total": 0}
        all_uids = data[0].split()
        total = len(all_uids)
        start = max(0, total - page * per_page)
        end = max(0, total - (page - 1) * per_page)
        page_uids = all_uids[start:end] if start < end else []
        page_uids = list(reversed(page_uids))
        items = []
        for uid in page_uids:
            items.append(_fetch_one_inbox(imap, uid))
        imap.logout()
        return {"items": [i for i in items if i], "total": total, "page": page, "per_page": per_page, "pages": max(1, (total + per_page - 1) // per_page)}
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return {"error": str(e), "items": [], "total": 0}


def _fetch_one_inbox(imap, uid):
    """Fetch one email's metadata + attachment count for inbox list."""
    try:
        status, msg_data = imap.fetch(uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODYSTRUCTURE)")
        if status != "OK":
            return None
        raw_header = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
        msg = email.message_from_bytes(raw_header)
        subject = _decode_mime_header(msg.get("Subject", _("(无主题)")))
        _from = _decode_mime_header(msg.get("From", ""))
        date_str = msg.get("Date", "")

        has_attachments = False
        if len(msg_data[0]) > 2:
            bodystructure = str(msg_data[0][2])
            has_attachments = '("attachment"' in bodystructure.lower() or 'name="' in bodystructure.lower()

        raw_flags = msg_data[0][0] if isinstance(msg_data[0], tuple) else b""
        return {
            "uid": int(uid.decode() if isinstance(uid, bytes) else uid),
            "from": _from,
            "subject": subject,
            "date": date_str,
            "is_seen": b"\\Seen" in raw_flags if isinstance(raw_flags, bytes) else False,
            "has_attachments": has_attachments,
        }
    except:
        return None


def read_email(uid):
    try:
        imap = _connect_imap()
    except Exception as e:
        return {"error": _("IMAP 连接失败: {}").format(e)}
    try:
        uid_bytes = str(uid).encode() if isinstance(uid, int) else uid.encode() if isinstance(uid, str) else uid
        status, msg_data = imap.uid("fetch", uid_bytes, "(BODY[])")
        if status != "OK":
            imap.logout()
            return {"error": _("无法读取邮件")}
        raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
        msg = email.message_from_bytes(raw_email)
        subject = _decode_mime_header(msg.get("Subject", _("(无主题)")))
        _from = _decode_mime_header(msg.get("From", ""))
        _to = _decode_mime_header(msg.get("To", ""))
        _cc = _decode_mime_header(msg.get("Cc", ""))
        date_str = msg.get("Date", "")
        body_plain, body_html = _get_email_body(msg)
        attachments = _get_attachments_from_msg(msg)
        imap.uid("store", uid_bytes, "+FLAGS", "\\Seen")
        imap.logout()
        return {
            "uid": int(uid) if isinstance(uid, int) else uid,
            "from": _from, "to": _to, "cc": _cc,
            "subject": subject, "date": date_str,
            "body": body_plain, "body_html": body_html,
            "attachments": attachments,
        }
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return {"error": str(e)}


def get_attachment(uid, filename):
    """Extract a specific attachment from an email by UID and filename."""
    try:
        imap = _connect_imap()
    except Exception as e:
        return None, str(e)
    try:
        uid_bytes = str(uid).encode() if isinstance(uid, int) else uid.encode() if isinstance(uid, str) else uid
        status, msg_data = imap.uid("fetch", uid_bytes, "(BODY[])")
        if status != "OK":
            imap.logout()
            return None, _("无法读取邮件")
        raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
        msg = email.message_from_bytes(raw_email)
        attachments = _get_attachments_from_msg(msg)
        imap.logout()
        for att in attachments:
            if att["filename"] == filename and not att.get("too_large"):
                data = base64.b64decode(att["data"])
                return data, att["content_type"]
        return None, _("附件不存在")
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return None, str(e)


# ─── SMTP Public API ───────────────────────────────────────────────────────

def send_email(to_addr, subject, body_text, body_html=None, cc=None, reply_to=None, attachments=None):
    """Send email with optional HTML body, CC, reply-to, and file attachments.

    Args:
        to_addr: str or list of str — recipient(s)
        subject: str
        body_text: str — plain text body
        body_html: str — optional HTML body
        cc: str or list of str — optional CC recipients
        reply_to: str — optional Reply-To address
        attachments: list of {"filename": str, "data": base64_str, "content_type": str}

    Returns:
        (success: bool, message: str)
    """
    cfg = _get_mail_config()
    if not cfg['smtp_user'] or not cfg['smtp_pass']:
        return False, _("SMTP 未配置 (请先设置 SMTP_USER/SMTP_PASS 环境变量)")

    if isinstance(to_addr, str):
        to_addr = [to_addr]

    # Validate attachment sizes
    total_attach_size = 0
    if attachments:
        for att in attachments:
            data = base64.b64decode(att["data"]) if isinstance(att["data"], str) else att["data"]
            total_attach_size += len(data)
            if len(data) > _MAX_ATTACHMENT_SIZE:
                return False, _("附件 {} 超过 10MB 限制").format(att['filename'])
    if total_attach_size > 50 * 1024 * 1024:
        return False, _("附件总大小超过 50MB 限制")

    # Build message
    if attachments:
        msg = MIMEMultipart("mixed")
        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)
        body_container = msg_alt
    else:
        msg = MIMEMultipart("alternative")
        body_container = msg

    msg["Subject"] = subject
    msg["From"] = cfg['smtp_from']
    msg["To"] = ", ".join(to_addr)
    msg["Date"] = email.utils.formatdate(localtime=True)

    if cc:
        if isinstance(cc, str):
            cc = [cc]
        msg["Cc"] = ", ".join(cc)
        to_addr = list(to_addr) + cc

    if reply_to:
        msg["Reply-To"] = reply_to

    if body_html:
        body_container.attach(MIMEText(body_text, "plain", "utf-8"))
        body_container.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        body_container.attach(MIMEText(body_text, "plain", "utf-8"))

    # Attachments
    if attachments:
        for att in attachments:
            data = base64.b64decode(att["data"]) if isinstance(att["data"], str) else att["data"]
            part = MIMEApplication(data, Name=att["filename"])
            part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
            msg.attach(part)

    try:
        if cfg['smtp_port'] == 465:
            with smtplib.SMTP_SSL(cfg['smtp_host'], cfg['smtp_port'], timeout=15) as server:
                server.login(cfg['smtp_user'], cfg['smtp_pass'])
                server.sendmail(cfg['smtp_from'], to_addr, msg.as_string())
        else:
            with smtplib.SMTP(cfg['smtp_host'], cfg['smtp_port'], timeout=15) as server:
                server.starttls()
                server.login(cfg['smtp_user'], cfg['smtp_pass'])
                server.sendmail(cfg['smtp_from'], to_addr, msg.as_string())

        # Record to plugin's independent PG schema (email)
        from .models import get_email_db
        with get_email_db() as db:
            db.execute(
                "INSERT INTO email_sent (from_addr, to_addr, subject, body_text, body_html) VALUES (%s, %s, %s, %s, %s)",
                (cfg['smtp_from'], ", ".join(to_addr), subject, body_text, body_html)
            )
            db.commit()

        logger.info(f"Email sent to {to_addr}: {subject}")
        return True, _("发送成功")

    except smtplib.SMTPAuthenticationError:
        logger.error(_("SMTP 认证失败"))
        return False, _("SMTP 认证失败，请检查 SMTP_USER/SMTP_PASS")
    except smtplib.SMTPException as e:
        logger.error(_("SMTP 发送失败: {}").format(e))
        return False, _("SMTP 错误: {}").format(e)
    except Exception as e:
        logger.error(_("邮件发送异常: {}").format(e))
        return False, _("发送异常: {}").format(e)


def get_sent_emails(page=1, per_page=20):
    """从独立 PG schema (email) 查询已发送邮件列表"""
    from .models import get_email_db
    with get_email_db() as db:
        count = db.execute("SELECT COUNT(*) FROM email_sent").fetchone()['count']
        offset = (page - 1) * per_page
        rows = db.execute(
            "SELECT * FROM email_sent ORDER BY sent_at DESC LIMIT %s OFFSET %s",
            (per_page, offset)
        ).fetchall()
        items = [dict(r) for r in rows]
    return {"items": items, "total": count, "page": page, "per_page": per_page, "pages": max(1, (count + per_page - 1) // per_page)}


def send_contact_email(name, email_addr, subject, message):
    """发送联系表单邮件到管理员。"""
    admin_email = os.environ.get("CONTACT_TO", "")
    full_subject = _("[联系表单] {}").format(subject)

    try:
        # brand_service 来自主系统，这是跨模块调用（非数据库依赖）
        from services.brand_service import get_brand_settings
        brand = get_brand_settings() or {}
    except Exception:
        brand = {}
    site_name_cn = brand.get('site_name_cn', '') or ''
    site_name_en = brand.get('site_name_en', '') or ''

    body_text = _("""来自 {} 联系表单

姓名: {}
邮箱: {}
主题: {}
---
{}
""").format(site_name_cn or site_name_en or '', name, email_addr, subject, message)

    body_html = (
        '<!DOCTYPE html><html><body style="font-family:sans-serif;'
        'color:#333;max-width:600px;margin:20px auto">'
        + _('<h2 style="color:#00d4aa">📬 来自 {} 联系表单</h2>').format(site_name_en or site_name_cn or "")
        + _('<table style="width:100%;border-collapse:collapse">'
        '<tr><td style="padding:8px;color:#888">姓名</td><td style="padding:8px">{}</td></tr>'
        '<tr><td style="padding:8px;color:#888">邮箱</td><td style="padding:8px"><a href="mailto:{}">{}</a></td></tr>'
        '<tr><td style="padding:8px;color:#888">主题</td><td style="padding:8px">{}</td></tr>'
        '</table>').format(name, email_addr, email_addr, subject)
        + '<div style="margin-top:16px;padding:16px;background:#f5f5f5;border-radius:8px">{}</div>'.format(message)
        + '</body></html>'
    )

    return send_email(
        to_addr=admin_email,
        subject=full_subject,
        body_text=body_text,
        body_html=body_html,
        reply_to=email_addr,
    )