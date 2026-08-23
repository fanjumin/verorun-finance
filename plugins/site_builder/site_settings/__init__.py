#!/usr/bin/env python3
"""Site Settings — 统一站点配置模块（替代 brand_settings + header_nav + footer_* + themes）"""

from flask import Blueprint

site_settings_bp = Blueprint('site_settings', __name__, url_prefix='/admin/site-settings')


def register_site_settings_bp():
    """注册站点设置路由并返回蓝图（由插件 SiteBuilderPlugin.register_routes 调用）。

    延迟到 register_routes 阶段注册，避免与插件根级 routes.py 循环导入。
    """
    from .routes import register_routes
    register_routes(site_settings_bp)
    return site_settings_bp
