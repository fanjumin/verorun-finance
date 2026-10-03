#!/usr/bin/env python3
"""
Mail Provider Registry — 邮件服务商预置清单（单一数据源）
=========================================================
预置国内外常用邮件服务商的 SMTP/IMAP 服务器参数，实现"用户只填写
邮箱账号（+密码）即可完成邮件服务器配置"的快捷设置。

设计约定：
- 本文件是服务商数据的唯一来源；routes.py 的 GET /providers 接口与
  services.py 的域名推导均从此处读取，避免重复定义造成漂移。
- 域名推导只填充「空配置字段」，绝不覆盖用户在设置页显式填写的值。
- Proton Mail 无公网 SMTP/IMAP 服务，必须通过官方 Proton Bridge 提供
  的本机端口收发邮件，故其服务器地址指向 127.0.0.1 并附说明。
"""

# each item: id, name, domains, smtp_host, smtp_port, smtp_ssl,
#            imap_host, imap_port, imap_ssl, note (optional)
MAIL_PROVIDERS = [
    # ── 国内邮箱 ──────────────────────────────────────────────
    {
        "id": "aliyun",
        "name": "阿里邮箱",
        "domains": ["aliyun.com", "aliyunmail.com"],
        "smtp_host": "smtp.aliyun.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.aliyun.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "aliyun-qiye",
        "name": "阿里云企业邮箱",
        "domains": ["qiye.aliyun.com"],
        "smtp_host": "smtp.qiye.aliyun.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.qiye.aliyun.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "tencent-ex",
        "name": "腾讯企业邮箱",
        "domains": ["exmail.qq.com"],
        "smtp_host": "smtp.exmail.qq.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.exmail.qq.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "qq",
        "name": "QQ邮箱",
        "domains": ["qq.com"],
        "smtp_host": "smtp.qq.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.qq.com", "imap_port": 993, "imap_ssl": True,
        "note": "需在 QQ 邮箱网页端开启 SMTP/IMAP 服务并获取授权码",
    },
    {
        "id": "foxmail",
        "name": "Foxmail 邮箱",
        "domains": ["foxmail.com"],
        "smtp_host": "smtp.foxmail.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.foxmail.com", "imap_port": 993, "imap_ssl": True,
        "note": "密码处填 Foxmail 授权码而非登录密码",
    },
    {
        "id": "netease-163",
        "name": "网易 163 邮箱",
        "domains": ["163.com"],
        "smtp_host": "smtp.163.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.163.com", "imap_port": 993, "imap_ssl": True,
        "note": "需开启 SMTP/IMAP 服务并获取授权码",
    },
    {
        "id": "netease-126",
        "name": "网易 126 邮箱",
        "domains": ["126.com"],
        "smtp_host": "smtp.126.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.126.com", "imap_port": 993, "imap_ssl": True,
        "note": "需开启 SMTP/IMAP 服务并获取授权码",
    },
    {
        "id": "netease-188",
        "name": "网易 188 邮箱",
        "domains": ["188.com"],
        "smtp_host": "smtp.188.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.188.com", "imap_port": 993, "imap_ssl": True,
        "note": "需开启 SMTP/IMAP 服务并获取授权码",
    },
    {
        "id": "sina",
        "name": "新浪邮箱",
        "domains": ["sina.com", "sina.cn"],
        "smtp_host": "smtp.sina.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.sina.com", "imap_port": 993, "imap_ssl": True,
        "note": "需开启 SMTP/IMAP 服务，独立密码用于客户端登录",
    },
    {
        "id": "sohu",
        "name": "搜狐邮箱",
        "domains": ["sohu.com"],
        "smtp_host": "smtp.sohu.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.sohu.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "cmcc-139",
        "name": "中国移动 139 邮箱",
        "domains": ["139.com"],
        "smtp_host": "smtp.139.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.139.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "net263",
        "name": "263 企业邮箱",
        "domains": ["263.net", "263xmail.com"],
        "smtp_host": "smtp.263.net", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.263.net", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "cn-21cn",
        "name": "21CN 邮箱",
        "domains": ["21cn.com"],
        "smtp_host": "smtp.21cn.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.21cn.com", "imap_port": 993, "imap_ssl": True,
    },
    # ── 国外邮箱 ──────────────────────────────────────────────
    {
        "id": "gmail",
        "name": "Gmail",
        "domains": ["gmail.com", "googlemail.com"],
        "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_ssl": False,
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_ssl": True,
        "note": "2025-05 起关闭密码直连，需 OAuth 或 Google 应用专用密码",
    },
    {
        "id": "outlook",
        "name": "Outlook / Hotmail",
        "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com"],
        "smtp_host": "smtp-mail.outlook.com", "smtp_port": 587, "smtp_ssl": False,
        "imap_host": "outlook.office365.com", "imap_port": 993, "imap_ssl": True,
        "note": "开启两步验证后需应用专用密码；2022-10 起弃用 IMAP 基本认证",
    },
    {
        "id": "yahoo",
        "name": "Yahoo Mail",
        "domains": ["yahoo.com"],
        "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.mail.yahoo.com", "imap_port": 993, "imap_ssl": True,
        "note": "需开启两步验证并使用应用专用密码",
    },
    {
        "id": "icloud",
        "name": "iCloud Mail",
        "domains": ["icloud.com", "me.com", "mac.com"],
        "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_ssl": False,
        "imap_host": "imap.mail.me.com", "imap_port": 993, "imap_ssl": True,
        "note": "需使用 Apple ID 专用密码",
    },
    {
        "id": "aol",
        "name": "AOL Mail",
        "domains": ["aol.com"],
        "smtp_host": "smtp.aol.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.aol.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "zoho",
        "name": "Zoho Mail",
        "domains": ["zoho.com", "zohomail.com"],
        "smtp_host": "smtp.zoho.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.zoho.com", "imap_port": 993, "imap_ssl": True,
    },
    {
        "id": "yandex",
        "name": "Yandex Mail",
        "domains": ["yandex.com", "yandex.ru"],
        "smtp_host": "smtp.yandex.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.yandex.com", "imap_port": 993, "imap_ssl": True,
        "note": "需生成应用专用密码",
    },
    {
        "id": "fastmail",
        "name": "Fastmail",
        "domains": ["fastmail.com", "fastmail.fm"],
        "smtp_host": "smtp.fastmail.com", "smtp_port": 465, "smtp_ssl": True,
        "imap_host": "imap.fastmail.com", "imap_port": 993, "imap_ssl": True,
        "note": "需使用应用专用密码",
    },
    {
        "id": "proton",
        "name": "Proton Mail（需 Bridge）",
        "domains": ["proton.me", "protonmail.com", "protonmail.ch"],
        "smtp_host": "127.0.0.1", "smtp_port": 1025, "smtp_ssl": False,
        "imap_host": "127.0.0.1", "imap_port": 1143, "imap_ssl": False,
        "note": "Proton 无公网 SMTP/IMAP，须运行官方 Proton Bridge 后经本机 1025/1143 端口收发",
    },
]


def get_provider_by_domain(email_or_domain):
    """按域名匹配预置服务商。

    支持两种输入：完整邮箱地址（自动提取 @ 后域名）或裸域名。
    匹配规则：先精确匹配（如 qq.com），未命中再做最长后缀匹配
    （如 user@mail.qq.com → qq.com）。返回 provider dict 或 None。
    """
    if not email_or_domain:
        return None
    domain = str(email_or_domain).strip().lower()
    if '@' in domain:
        domain = domain.rsplit('@', 1)[1].strip()
    domain = domain.lstrip('.').strip()
    if not domain:
        return None

    # 1) 精确匹配
    for p in MAIL_PROVIDERS:
        if domain in p['domains']:
            return p

    # 2) 最长后缀匹配
    best, best_len = None, 0
    for p in MAIL_PROVIDERS:
        for d in p['domains']:
            if domain.endswith('.' + d) and len(d) > best_len:
                best, best_len = p, len(d)
    return best