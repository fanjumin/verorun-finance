#!/usr/bin/env python3
"""
兼容性重导出 — 邮件发送服务
============================
原 mail_service.py 已迁移至 plugins/email/services.py。
独立数据库 email.db，配置通过环境变量。
此文件保留向后兼容，所有导入重定向到插件。
"""
try:
    from plugins.email.services import (
        send_email,
        send_contact_email,
        get_smtp_config,
        CONFIG_DEFS,
    )
except ImportError:
    # verorun-pro 精简版无 plugins 目录
    CONFIG_DEFS = []