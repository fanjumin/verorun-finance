#!/usr/bin/env python3
"""
Site Domains Plugin — 子域名管理 + Nginx 配置生成
==================================================
仅【逻辑解耦】：后台 site_domains CRUD + Nginx 配置从 admin/app.py 迁入插件。

重要约束（与 im_gateway / social_push 不同）：
  - 【不使用独立库】。site_domains 表被中间件 site_domain_middleware 每请求读取，
    且有 FK → site_configs，必须继续留在主库。本插件通过 get_main_db() 读写主库。
  - 中间件 site_domain_middleware.py 完全不改，建表/种子留 database.py 不动。
"""

from i18n import _
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin


class SiteDomainsPlugin(BasePlugin):
    # 元数据（name/version/description/author）以 plugin.json 为唯一真源（§13.1），
    # 不再在类中重复声明，避免与 plugin.json 版本号冲突。

    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'

    def on_enable(self, registry):
        """启用（无独立库，不建表）"""
        print(_('[SiteDomainsPlugin] ✅ Subdomain management plugin is enabled'))
        return True

    def register_routes(self):
        """注册 Flask 路由（Caddy On-Demand TLS 校验端点）。
        CRUD 由 auth-center admin_bp 提供，本插件不重复注册。"""
        from .routes import caddy_check_bp
        return [caddy_check_bp]

    def on_disable(self, registry):
        print(_('[SiteDomainsPlugin] ⚠️ Subdomain management plugin is disabled'))
        return True
