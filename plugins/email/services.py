#!/usr/bin/env python3
"""
Email Service — 统一邮件服务（SMTP 发信 + IMAP 收信 + 附件）
==============================================================
合并原 email_client.py 和 mail_service.py，提供统一的邮件接口。
完全独立于主库，使用独立 PG schema: email + 环境变量配置。

配置来源优先级：环境变量 → PluginManager → system_config → 默认值 → 域名自动推导

环境变量              | 说明                    | 默认值
---------------------|-------------------------|--------------------------
SMTP_HOST            | SMTP 服务器              | （空，可由邮箱域名自动推导）
SMTP_PORT            | SMTP 端口                | （空，可由邮箱域名自动推导）
SMTP_USER            | SMTP 账号                | （必填）
SMTP_PASS            | SMTP 密码                | （必填）
SMTP_FROM            | 发件人地址              | 同 SMTP_USER
IMAP_HOST            | IMAP 服务器             | （空，可由邮箱域名自动推导）
IMAP_PORT            | IMAP 端口               | （空，可由邮箱域名自动推导）

> 快捷配置：用户在设置页填写邮箱账号后，系统会依据 mail_providers.py
> 预置的服务商清单自动补全 SMTP/IMAP 服务器与端口；自动推导只填充
> 空字段，不覆盖用户显式填写的配置。
"""

import os
import re
import hmac
import html
import email
import json
import quopri
import base64
import logging
import secrets
import smtplib
import ssl
import imaplib
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from i18n import _
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.application import MIMEApplication
from email.utils import formataddr, parsedate_to_datetime, parseaddr
from plugins.email.mail_providers import get_provider_by_domain  # 服务商域名推导（快捷配置）

logger = logging.getLogger(__name__)

_MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10MB

# ── 邮件头注入防护（EM-4）──
_HEADER_CTRL_RE = re.compile(r'[\r\n\x00]')
_HEADER_CRLF_RE = re.compile(r'[\r\n]+')


def _clean_header(value, limit: int = 900) -> str:
    """头部字段值净化：CR/LF 折叠为空格、剔除 NUL，阻断 SMTP 头注入。"""
    v = _HEADER_CRLF_RE.sub(' ', str(value if value is not None else ''))
    return v.replace('\x00', '').strip()[:limit]


def _clean_addr(value):
    """地址字段净化：含 CR/LF/NUL 直接判非法；按逗号/分号拆分逐项校验。

    返回 None 表示非法；合法时返回原字符串（保留显示名与原文分隔符，
    因此不改变既有多收件人语义）。
    """
    raw = str(value if value is not None else '')
    if _HEADER_CTRL_RE.search(raw):
        return None
    parts = [p.strip() for p in re.split(r'[,\uff0c;；]', raw) if p.strip()]
    if not parts:
        return None
    for _p in parts:
        _name, _addr = parseaddr(_p)
        if not _addr or '@' not in _addr:
            return None
    return raw.strip()


# ── Config keys ──
_MAIL_KEYS = ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'smtp_from', 'imap_host', 'imap_port']
_DEFAULTS = {
    'smtp_host': '', 'smtp_port': '',
    'smtp_user': '', 'smtp_pass': '', 'smtp_from': '',
    'imap_host': '', 'imap_port': '',
}
_ENV_MAP = {
    'smtp_host': 'SMTP_HOST', 'smtp_port': 'SMTP_PORT',
    'smtp_user': 'SMTP_USER', 'smtp_pass': 'SMTP_PASS',
    'smtp_from': 'SMTP_FROM', 'imap_host': 'IMAP_HOST', 'imap_port': 'IMAP_PORT',
}

CONFIG_DEFS = {
    'smtp_host':  {'label': _('SMTP 服务器'),    'default': '',   'sensitive': False},
    'smtp_port':  {'label': _('SMTP 端口'),      'default': '',   'sensitive': False},
    'smtp_user':  {'label': _('SMTP 账号'),      'default': '',   'sensitive': False},
    'smtp_pass':  {'label': _('SMTP 密码'),      'default': '',   'sensitive': True},
    'smtp_from':  {'label': _('发件人地址'),      'default': '',   'sensitive': False},
    'imap_host':  {'label': _('IMAP 服务器'),     'default': '',   'sensitive': False},
    'imap_port':  {'label': _('IMAP 端口'),       'default': '',   'sensitive': False},
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


# ─── 安全开关（EM-7 / EM-9）：独立于 _MAIL_KEYS，不改动设置页表单字段 ───
_SECURITY_ENV = {
    'blocked_attachment_exts': 'EMAIL_BLOCKED_ATTACHMENT_EXTS',
    'allow_private_targets': 'EMAIL_ALLOW_PRIVATE_TARGETS',
    'daily_send_quota': 'EMAIL_DAILY_SEND_QUOTA',
    'recipient_domain_allowlist': 'EMAIL_RECIPIENT_DOMAIN_ALLOWLIST',
}
_SECURITY_DEFAULTS = {
    'blocked_attachment_exts': '.exe,.bat,.cmd,.scr,.js,.vbs,.ps1,.jar,.msi',
    'allow_private_targets': '',
    # EM-3：每日发信配额（0 = 不限制）；收件人域白名单（空 = 不限制）
    'daily_send_quota': '200',
    'recipient_domain_allowlist': '',
}


def get_security_option(name):
    """读取邮件插件安全开关：环境变量 > 插件配置 > 插件默认值。"""
    env_key = _SECURITY_ENV.get(name)
    if env_key:
        val = str(os.environ.get(env_key, '') or '').strip()
        if val:
            return val
    val = _get_plugin_manager_config().get(name)
    if val is not None and str(val).strip() != '':
        return val
    return _SECURITY_DEFAULTS.get(name, '')


def blocked_attachment_exts():
    """附件扩展名黑名单集合（EM-7）；返回空集合表示不拦截任何类型。"""
    raw = str(get_security_option('blocked_attachment_exts') or '')
    out = set()
    for item in raw.split(','):
        item = item.strip().lower()
        if item:
            out.add(item if item.startswith('.') else '.' + item)
    return out


# ─── 发信控制面（EM-3）：收件人域白名单 + 每日配额 ─────────────────────

def _recipient_addresses(value):
    """把 str / list 收件人字段拆成纯 addr-spec 列表（小写，忽略显示名）。"""
    raw = [value] if isinstance(value, str) else list(value or [])
    out = []
    for item in raw:
        for part in re.split(r'[,\uff0c;；]', str(item or '')):
            _name, addr = parseaddr(part.strip())
            if addr and '@' in addr:
                out.append(addr.lower())
    return out


def allowed_recipient_domains():
    """收件人域白名单集合；返回空集合表示不限制目标域。"""
    raw = str(get_security_option('recipient_domain_allowlist') or '')
    return {d.strip().lower().lstrip('@') for d in raw.split(',') if d.strip()}


def daily_send_quota():
    """每日发信配额（0 = 不限制）。"""
    try:
        return max(0, int(str(get_security_option('daily_send_quota') or '0').strip() or 0))
    except (TypeError, ValueError):
        return 0


def daily_sent_count():
    """今日已成功发送条数（以 email_sent 实际记录计数，不新增表）。

    独立 schema 不可用时返回 None，调用侧据此 fail-open 放行，
    避免监控数据缺失直接阻断业务发信。
    """
    try:
        from .models import get_email_db
        with get_email_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM email_sent WHERE sent_at >= CURRENT_DATE").fetchone()
        return int(row['c']) if row else 0
    except Exception as e:
        logger.warning(_("Failed to read daily send quota, allowing this send: {}").format(e))
        return None


# ─── MCP 凭据桥（EM-1b）：子进程不再持有任何邮件凭据 ───────────────────
_bridge_lock = threading.Lock()
_bridge_server = None
_bridge_url = ''
_bridge_token = ''
_bridge_app = None


class _BridgeHandler(BaseHTTPRequestHandler):
    """仅监听 127.0.0.1 的内部凭据桥（回环绑定 + 固定随机令牌校验）。"""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler 约定命名
        if not _bridge_token or not hmac.compare_digest(
                str(self.headers.get('X-Email-Bridge-Token') or ''), _bridge_token):
            self._reply(403, {'ok': False, 'error': 'forbidden'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > 8 * 1024 * 1024:
                raise ValueError('invalid payload length')
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            if _bridge_app is None:
                result = _bridge_dispatch(payload)
            else:
                with _bridge_app.app_context():
                    result = _bridge_dispatch(payload)
            self._reply(200, {'ok': True, 'result': result})
        except Exception as e:
            logger.warning(f'[email] mcp bridge call failed: {e}')
            self._reply(200, {'ok': False, 'error': str(e)})

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 桥流量不污染服务日志


def _ensure_contact_to():
    """联系表单收件人回落：env 缺失时从主库 system_config 读取。

    旧实现由主进程把 CONTACT_TO 注入 MCP 子进程 env；EM-1(b) 后发送在主进程
    执行，故在此补齐同一回落，避免联系表单邮件因 env 缺失而发不出去。
    """
    if os.environ.get('CONTACT_TO'):
        return
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_config"
                " WHERE key IN ('contact_to', 'contact_email') LIMIT 1").fetchone()
        if row and row['value']:
            os.environ['CONTACT_TO'] = str(row['value'])
    except Exception:
        pass


def _bridge_dispatch(payload):
    """在主进程内执行子进程提交的操作（工具名白名单，不接受任意调用）。"""
    tool = str((payload or {}).get('tool') or '')
    args = (payload or {}).get('args') or {}
    cfg = _get_mail_config()
    if tool == 'email_send':
        # EM-3：每日配额只约束 Agent（MCP）发信路径；后台手动发信与业务钩子
        #      发信不受限。计数取自 email_sent 实际记录，不新增表。
        _quota = daily_send_quota()
        if _quota:
            _used = daily_sent_count()
            if _used is not None and _used >= _quota:
                return [False, _("Daily send quota exceeded") + f" ({_used}/{_quota})"]
        return list(send_email(
            args.get('to'), args.get('subject'), args.get('body'),
            body_html=args.get('body_html'), cc=args.get('cc'),
            bcc=args.get('bcc'), attachments=args.get('attachments'), cfg=cfg))
    if tool == 'email_send_contact':
        _ensure_contact_to()
        return list(send_contact_email(
            args.get('name'), args.get('email'), args.get('subject'),
            args.get('message'), cfg=cfg))
    if tool == 'email_get_config':
        safe = dict(cfg)
        if safe.get('smtp_pass'):
            safe['smtp_pass'] = '********'
        return safe
    raise ValueError(f'unsupported bridge tool: {tool}')


def get_mcp_bridge_endpoint(app=None):
    """启动（幂等）仅监听 127.0.0.1 的邮件凭据桥，返回 (url, token)。

    EM-1(b)：MCP 子进程只拿到桥地址与桥生命周期内固定的 256bit 随机令牌
    （非每请求轮换），SMTP/IMAP 凭据始终留在主进程内存；桥不可用时返回
    空值，调用方据此拒绝发信，而非回退为向子进程注入明文密码。
    """
    global _bridge_server, _bridge_url, _bridge_token, _bridge_app
    with _bridge_lock:
        if _bridge_server is None:
            try:
                srv = ThreadingHTTPServer(('127.0.0.1', 0), _BridgeHandler)
                srv.daemon_threads = True
                threading.Thread(target=srv.serve_forever, daemon=True).start()
            except Exception as e:
                logger.warning(f'[email] mcp bridge start failed: {e}')
                return '', ''
            _bridge_server = srv
            _bridge_url = 'http://127.0.0.1:%d/send' % srv.server_address[1]
            _bridge_token = secrets.token_hex(32)
            _bridge_app = app
        return _bridge_url, _bridge_token


def _apply_provider_derivation(cfg):
    """根据账号邮箱域名推导 SMTP/IMAP 服务器参数（仅填充空字段）。

    当 smtp_user（或 smtp_from）能匹配到 mail_providers.py 预置的
    服务商时，自动补全 host/port。已在环境变量、PluginManager、
    system_config 中显式配置过的字段保持原值，不被覆盖。
    """
    account = str(cfg.get('smtp_user') or cfg.get('smtp_from') or '').strip()
    provider = get_provider_by_domain(account) if account else None
    if not provider:
        return
    fills = {
        'smtp_host': provider['smtp_host'],
        'smtp_port': str(provider['smtp_port']),
        'imap_host': provider['imap_host'],
        'imap_port': str(provider['imap_port']),
    }
    for k, v in fills.items():
        if not str(cfg.get(k) or '').strip():
            cfg[k] = v


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

    # 4. 域名自动推导：账号匹配预置服务商时补全空白的服务器/端口字段
    _apply_provider_derivation(cfg)

    if not cfg['smtp_from']:
        cfg['smtp_from'] = cfg['smtp_user']
    if not cfg['smtp_user']:
        cfg['smtp_user'] = cfg['smtp_from']
    try:
        cfg['smtp_port'] = int(cfg['smtp_port'])
    except (ValueError, TypeError):
        cfg['smtp_port'] = 0
    try:
        cfg['imap_port'] = int(cfg['imap_port'])
    except (ValueError, TypeError):
        cfg['imap_port'] = 0
    # EM-1(a)：密码以密文落盘，读取时解密；fail-open 保证存量明文原样可用
    if cfg.get('smtp_pass'):
        try:
            from plugins.email.crypto import decrypt as _decrypt_secret
            cfg['smtp_pass'] = _decrypt_secret(str(cfg['smtp_pass']))
        except Exception:
            pass
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

_SEARCH_FALLBACK_LIMIT = 500  # 服务端 SEARCH 不可用时，客户端内存过滤的最大取样封数


def _imap_search_keyword(imap, kw):
    """按关键字搜索（发件人/主题/正文）。

    优先服务端 SEARCH（CHARSET UTF-8，FROM/SUBJECT/TEXT 并集，可命中正文）；
    服务端不支持（IMAP4.error / BAD）时，降级为客户端内存过滤
    （最近 N 封的发件人/主题，兼容优先，见 §6 验收）。
    """
    uidset = set()
    service_ok = False
    try:
        kw_b = kw.encode("utf-8")
        for criterion in ("FROM", "SUBJECT", "TEXT"):
            try:
                status, data = imap.uid("search", "UTF-8", criterion, kw_b)
            except imaplib.IMAP4.error:
                continue
            if status == "OK" and data and data[0]:
                service_ok = True
                for u in data[0].split():
                    uidset.add(u)
    except Exception:
        service_ok = False
    if service_ok and uidset:
        return sorted(uidset, key=lambda x: int(x))
    # 客户端内存过滤回退：遍历最近 _SEARCH_FALLBACK_LIMIT 封的头部
    try:
        status, data = imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        all_uids = data[0].split()[-_SEARCH_FALLBACK_LIMIT:]
        matched = []
        kw_l = kw.lower()
        for u in all_uids:
            try:
                meta = _fetch_one_inbox(imap, u)
            except Exception:
                continue
            if not meta:
                continue
            if kw_l in str(meta.get("subject") or "").lower() or kw_l in str(meta.get("from") or "").lower():
                matched.append(u)
        return matched
    except Exception:
        return []


def fetch_inbox(page=1, per_page=20, keyword=''):
    """拉取收件箱列表；keyword 非空时按发件人/主题/正文过滤（v1.7.0）。

    UID 语义与 read_email / 批量操作（delete/mark/move）保持一致。
    """
    try:
        imap = _connect_imap()
    except Exception as e:
        return {"error": _("IMAP 连接失败: {}").format(e), "items": [], "total": 0}
    try:
        kw = (keyword or '').strip()
        if kw:
            all_uids = _imap_search_keyword(imap, kw)
        else:
            status, data = imap.uid("search", None, "ALL")
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
        status, msg_data = imap.uid('fetch', uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODYSTRUCTURE)")
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

def send_email(to_addr, subject, body_text, body_html=None, cc=None, bcc=None, reply_to=None, attachments=None, cfg=None):
    """Send email with optional HTML body, CC, BCC, reply-to, and file attachments.

    Args:
        to_addr: str or list of str — recipient(s)
        subject: str
        body_text: str — plain text body
        body_html: str — optional HTML body
        cc: str or list of str — optional CC recipients（写入 MIME Cc 头）
        bcc: str or list of str — optional BCC recipients（仅作发送目标，不写入 MIME 头）
        reply_to: str — optional Reply-To address
        attachments: list of {"filename": str, "data": base64_str, "content_type": str}
        cfg: dict — 预构造配置（MCP 子进程注入，跳过 DB/环境读取）；默认 None 走 _get_mail_config()

    Returns:
        (success: bool, message: str)
    """
    if cfg is None:
        cfg = _get_mail_config()
    if not cfg['smtp_user'] or not cfg['smtp_pass']:
        return False, _("SMTP 未配置 (请先设置 SMTP_USER/SMTP_PASS 环境变量)")

    # 收件人净化（EM-4）：str / list 归一并逐项校验，任一非法即整体拒绝
    _raw_to = [to_addr] if isinstance(to_addr, str) else list(to_addr or [])
    _clean_to = [_clean_addr(a) for a in _raw_to]
    if not _clean_to or any(a is None for a in _clean_to):
        return False, _("Invalid recipient address")
    to_addr = _clean_to

    # 发信控制面（EM-3）：收件人域白名单 —— 非空白名单未命中即整封拒绝
    _allow_domains = allowed_recipient_domains()
    if _allow_domains:
        _targets = [a.rsplit('@', 1)[-1] for a in _recipient_addresses(to_addr)]
        _targets += [a.rsplit('@', 1)[-1] for a in _recipient_addresses(cc)]
        _targets += [a.rsplit('@', 1)[-1] for a in _recipient_addresses(bcc)]
        _blocked = sorted({d for d in _targets if d not in _allow_domains})
        if _blocked:
            return False, _("Recipient domain not allowed") + ": " + ", ".join(_blocked)

    # 注：每日配额仅对 MCP（Agent）发信路径生效，见 _bridge_dispatch()；
    #     后台手动发信与业务钩子发信不受配额约束。

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

    msg["Subject"] = _clean_header(subject)
    msg["From"] = cfg.get('smtp_from') or cfg.get('smtp_user')
    msg["To"] = ", ".join(to_addr)
    msg["Date"] = email.utils.formatdate(localtime=True)

    if cc:
        _raw_cc = [cc] if isinstance(cc, str) else list(cc)
        _clean_cc = [_clean_addr(a) for a in _raw_cc]
        if any(a is None for a in _clean_cc):
            return False, _("Invalid cc address")
        cc = _clean_cc
        msg["Cc"] = ", ".join(cc)
        to_addr = list(to_addr) + cc

    if bcc:
        # BCC 仅作发送目标（RCPT），不写入 MIME 头，避免收件人看到密送列表
        _raw_bcc = [bcc] if isinstance(bcc, str) else list(bcc)
        _clean_bcc = [_clean_addr(a) for a in _raw_bcc]
        if any(a is None for a in _clean_bcc):
            return False, _("Invalid bcc address")
        bcc = _clean_bcc
        to_addr = list(to_addr) + bcc

    if reply_to:
        _clean_reply = _clean_addr(reply_to)
        if _clean_reply:
            msg["Reply-To"] = _clean_reply

    if body_html:
        body_container.attach(MIMEText(body_text, "plain", "utf-8"))
        body_container.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        body_container.attach(MIMEText(body_text, "plain", "utf-8"))

    # Attachments
    if attachments:
        _blocked_exts = blocked_attachment_exts()
        for att in attachments:
            data = base64.b64decode(att["data"]) if isinstance(att["data"], str) else att["data"]
            _fname = _clean_header(att.get("filename") or "attachment", limit=200)
            # EM-7：附件类型黑名单（可配置，空 = 不拦截）。按净化后的文件名逐段取扩展名，
            # 避免 '\r\n' 折叠成空格后被结尾扩展名掩盖（如 'evil.exe .pdf'）。
            if _blocked_exts:
                _hit = next((e for e in
                             (os.path.splitext(seg)[1].lower() for seg in _fname.split())
                             if e and e in _blocked_exts), '')
                if _hit:
                    return False, _("Blocked attachment type") + ": " + _hit
            part = MIMEApplication(data, Name=_fname)
            # 交由 email 库按 RFC 2231/2047 正确编码，避免引号/换行破坏头结构（EM-4）
            part.add_header("Content-Disposition", "attachment", filename=_fname)
            msg.attach(part)

    try:
        if cfg['smtp_port'] == 465:
            with smtplib.SMTP_SSL(cfg['smtp_host'], cfg['smtp_port'], timeout=15) as server:
                server.login(cfg['smtp_user'], cfg['smtp_pass'])
                server.sendmail(cfg.get('smtp_from') or cfg.get('smtp_user'), to_addr, msg.as_string())
        else:
            with smtplib.SMTP(cfg['smtp_host'], cfg['smtp_port'], timeout=15) as server:
                server.starttls()
                server.login(cfg['smtp_user'], cfg['smtp_pass'])
                server.sendmail(cfg.get('smtp_from') or cfg.get('smtp_user'), to_addr, msg.as_string())

        # Record to plugin's independent PG schema (email) —
        # MCP 子进程无 DB 能力时记录失败不影响发送结果（仅告警，v1.7.0）
        try:
            from .models import get_email_db
            with get_email_db() as db:
                db.execute(
                    "INSERT INTO email_sent (from_addr, to_addr, subject, body_text, body_html) VALUES (%s, %s, %s, %s, %s)",
                    (cfg['smtp_from'], ", ".join(to_addr), subject, body_text, body_html)
                )
                db.commit()
        except Exception as _db_e:
            logger.warning(_("邮件已发送，但记录已发送日志失败: {}").format(_db_e))

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


# ─── Drafts（草稿箱，v1.7.0） ────────────────────────────────────────────

def save_draft(draft_id=None, to_addr='', cc_addr='', bcc_addr='', subject='',
               body_text='', body_html='', attachments=None):
    """新建或更新草稿；attachments 以 JSON 文本列存储（仅存引用，不存二进制）。"""
    import json as _json
    from .models import get_email_db
    att_json = _json.dumps(attachments or [], ensure_ascii=False)
    with get_email_db() as db:
        if draft_id:
            row = db.execute("SELECT id FROM email_drafts WHERE id=%s", (draft_id,)).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE email_drafts SET to_addr=%s, cc_addr=%s, bcc_addr=%s, subject=%s, "
                "body_text=%s, body_html=%s, attachments=%s, updated_at=NOW() WHERE id=%s",
                (to_addr, cc_addr, bcc_addr, subject, body_text, body_html, att_json, draft_id))
            db.commit()
            return draft_id
        row = db.execute(
            "INSERT INTO email_drafts (to_addr, cc_addr, bcc_addr, subject, body_text, body_html, attachments) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (to_addr, cc_addr, bcc_addr, subject, body_text, body_html, att_json)).fetchone()
        db.commit()
        return row['id'] if row else None


def list_drafts(page=1, per_page=50):
    """草稿列表（更新序倒排）。"""
    import json as _json
    from .models import get_email_db
    with get_email_db() as db:
        count = db.execute("SELECT COUNT(*) AS count FROM email_drafts").fetchone()['count']
        rows = db.execute(
            "SELECT * FROM email_drafts ORDER BY updated_at DESC LIMIT %s OFFSET %s",
            (per_page, (page - 1) * per_page)).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        try:
            item['attachments'] = _json.loads(item.get('attachments') or '[]')
        except Exception:
            item['attachments'] = []
        items.append(item)
    return {"items": items, "total": count, "page": page, "per_page": per_page,
            "pages": max(1, (count + per_page - 1) // per_page)}


def delete_draft(draft_id):
    """删除草稿；不存在返回 False。"""
    from .models import get_email_db
    with get_email_db() as db:
        row = db.execute("SELECT id FROM email_drafts WHERE id=%s", (draft_id,)).fetchone()
        if not row:
            return False
        db.execute("DELETE FROM email_drafts WHERE id=%s", (draft_id,))
        db.commit()
    return True


# ─── Inbox batch operations（信箱管理，v1.7.0） ────────────────────────

def list_folders():
    """列出邮箱文件夹（IMAP LIST）；失败返回 {"error", "folders": []}。"""
    try:
        imap = _connect_imap()
    except Exception as e:
        return {"error": _("IMAP 连接失败: {}").format(e), "folders": []}
    try:
        status, data = imap.list()
        folders = []
        if status == "OK":
            for line in data:
                if not line:
                    continue
                line_s = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                parts = line_s.split('"')
                if len(parts) >= 3:
                    folders.append(parts[-2].strip())
                else:
                    fields = line_s.split()
                    if fields:
                        folders.append(fields[-1].strip().strip('"'))
        imap.logout()
        return {"folders": folders}
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return {"error": str(e), "folders": []}


def _uid_bytes(uids):
    """规范化 UID 列表为 IMAP UID SET（bytes）。"""
    norm = []
    for u in uids:
        try:
            norm.append(str(int(u)))
        except (TypeError, ValueError):
            continue
    return ','.join(norm).encode()


def delete_emails(uids):
    r"""批量删除（UID STORE \Deleted + EXPUNGE，立即生效）。"""
    try:
        imap = _connect_imap()
    except Exception as e:
        return False, _("IMAP 连接失败: {}").format(e)
    try:
        ub = _uid_bytes(uids)
        if not ub:
            return False, _("无效的邮件 UID")
        status, _resp = imap.uid('store', ub, '+FLAGS', '(\\Deleted)')
        if status != 'OK':
            return False, _("标记删除失败")
        imap.expunge()
        imap.logout()
        return True, _("已删除 {} 封邮件").format(len(uids))
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return False, str(e)


def mark_read(uids, read=True):
    r"""批量标记已读/未读（UID STORE ±\Seen）。"""
    try:
        imap = _connect_imap()
    except Exception as e:
        return False, _("IMAP 连接失败: {}").format(e)
    try:
        ub = _uid_bytes(uids)
        if not ub:
            return False, _("无效的邮件 UID")
        op = '+FLAGS' if read else '-FLAGS'
        status, _resp = imap.uid('store', ub, op, '(\\Seen)')
        imap.logout()
        if status != 'OK':
            return False, _("标记失败")
        return True, _("已标记为已读") if read else _("已标记为未读")
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return False, str(e)


def move_emails(uids, folder):
    r"""批量移动到目标文件夹（UID COPY + \Deleted + EXPUNGE）。"""
    try:
        imap = _connect_imap()
    except Exception as e:
        return False, _("IMAP 连接失败: {}").format(e)
    try:
        ub = _uid_bytes(uids)
        if not ub:
            return False, _("无效的邮件 UID")
        folders = list_folders()
        if folder != 'INBOX' and folder not in folders.get('folders', []):
            return False, _("目标文件夹不存在: {}").format(folder)
        status, _resp = imap.uid('copy', ub, folder)
        if status != 'OK':
            return False, _("复制到目标文件夹失败")
        imap.uid('store', ub, '+FLAGS', '(\\Deleted)')
        imap.expunge()
        imap.logout()
        return True, _("已移动 {} 封邮件到 {}").format(len(uids), folder)
    except Exception as e:
        try:
            imap.logout()
        except Exception:
            pass
        return False, str(e)


def send_contact_email(name, email_addr, subject, message, cfg=None):
    """发送联系表单邮件到管理员。

    cfg: dict — 预构造配置（MCP 子进程注入）；默认 None 走 _get_mail_config()。
    """
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
        # 用户提交内容一律 HTML 转义后再拼接（EM-6：阻断模板注入）
        + _('<h2 style="color:#00d4aa">📬 来自 {} 联系表单</h2>').format(html.escape(site_name_en or site_name_cn or ""))
        + _('<table style="width:100%;border-collapse:collapse">'
        '<tr><td style="padding:8px;color:#888">姓名</td><td style="padding:8px">{}</td></tr>'
        '<tr><td style="padding:8px;color:#888">邮箱</td><td style="padding:8px"><a href="mailto:{}">{}</a></td></tr>'
        '<tr><td style="padding:8px;color:#888">主题</td><td style="padding:8px">{}</td></tr>'
        '</table>').format(html.escape(name), html.escape(email_addr, quote=True),
                           html.escape(email_addr), html.escape(subject))
        + '<div style="margin-top:16px;padding:16px;background:#f5f5f5;border-radius:8px">{}</div>'.format(html.escape(message))
        + '</body></html>'
    )

    return send_email(
        to_addr=admin_email,
        subject=full_subject,
        body_text=body_text,
        body_html=body_html,
        reply_to=email_addr,
        cfg=cfg,
    )