#!/usr/bin/env python3
"""子域名识别中间件 — 根据 Host header 识别当前子域名站点

在每个请求处理前调用 resolve_current_site()，将结果注入 g 对象：
  - g.current_site:  当前站点的配置信息（theme_color / logo_url / name 等）
  - g.current_domain: 当前 site_domains 行完整数据
  - 未匹配到任何站点时均为 None（走默认逻辑）
"""

import os
from flask import g, request
from models import get_db


def resolve_current_site():
    """根据 Host header 查询当前子域名站点"""
    host = request.headers.get('Host', '').split(':')[0].lower()
    if not host or host.startswith('127.') or host == 'localhost':
        g.current_site = None
        g.current_domain = None
        return

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT sd.*, sc.name as site_name, sc.theme_color, sc.accent_color, "
                "sc.logo_url, sc.favicon_url, sc.tier "
                "FROM site_domains sd "
                "JOIN site_configs sc ON sc.id = sd.site_config_id "
                "WHERE sd.full_domain = %s AND sd.is_published = 1",
                (host,)
            ).fetchone()
    except Exception:
        g.current_site = None
        g.current_domain = None
        return

    if row:
        d = dict(row)
        g.current_domain = d
        svc_port = d.get('service_port')
        g.current_site = {
            'id': d['site_config_id'],
            'name': d['site_name'],
            'subdomain': d['subdomain'],
            'display_name': d['display_name'],
            'theme_color': d.get('theme_color', '#6366f1'),
            'accent_color': d.get('accent_color', '#8b5cf6'),
            'logo_url': d.get('logo_url', ''),
            'favicon_url': d.get('favicon_url', ''),
            'tier': d.get('tier', ''),
            'template': d.get('template', 'default'),
            'service_type': 'independent' if svc_port else 'content',
            'service_port': svc_port,
        }
    else:
        g.current_site = None
        g.current_domain = None
