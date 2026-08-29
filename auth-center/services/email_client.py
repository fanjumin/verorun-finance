#!/usr/bin/env python3
"""
兼容性重导出 — 邮件客户端
==========================
原 email_client.py 已迁移至 plugins/email/services.py。
独立数据库 email.db，配置通过环境变量。
此文件保留向后兼容，所有导入重定向到插件。
"""
try:
    from plugins.email.services import (
        fetch_inbox,
        read_email,
        get_attachment,
        send_email,
        get_sent_emails,
        get_smtp_config,
        _get_mail_config,
        _connect_imap,
        _decode_mime_header,
        _decode_body,
        _get_email_body,
        _get_attachments_from_msg,
        _fetch_one_inbox,
        _MAIL_KEYS,
        _DEFAULTS,
        _ENV_MAP,
        _MAX_ATTACHMENT_SIZE,
        CONFIG_DEFS,
    )
except ImportError:
    # verorun-pro 精简版无 plugins 目录
    CONFIG_DEFS = []