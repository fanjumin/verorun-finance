#!/usr/bin/env python3
"""
OAuth Login Config Plugin — 完整的第三方登录插件
===================================================
自 v0.13.5 起，本插件集成了：
1. 后台 OAuth 配置管理（CRUD + UI）
2. OAuth 登录/回调路由（抖音/微信/支付宝/Google/GitHub/Facebook/Telegram）
3. Provider 实现（多租户 DB + 环境变量兜底）

架构：所有 OAuth 相关代码收敛至本插件，auth-center 通过 try/except 调用。
"""
from i18n import _
import os
import sys

# 不添加 sys.path — 由宿主服务（admin / auth-center）负责路径设置

# 延迟加载 BasePlugin：admin 加载 oauth_cfg_bp 时 plugin_manager 可能未就绪
try:
    from plugin_manager.base import BasePlugin
    _BASE_CLS = BasePlugin
except ImportError:
    _BASE_CLS = object

_t = lambda text: text

def init_i18n(t_fn):
    global _t
    _t = t_fn

def _plugin_log(msg, level='info'):
    """Log to plugin-specific logger channel (§10.5)."""
    try:
        from plugin_manager.logger import get_plugin_logger
        logger = get_plugin_logger('oauth_config')
        getattr(logger, level)(msg)
    except Exception:
        print(f'[OauthConfigPlugin] {msg}')

class OauthConfigPlugin(_BASE_CLS):
    name = 'oauth_config'
    # NOTE: version is managed solely by plugin.json (§13.1)
    description = _('OAuth 第三方登录 — 完整的登录/回调/配置管理插件')
    author = 'VeroRun'

    # ── §4 Agent registration ──
    def register_agents(self):
        """Register agents into the Agent Matrix.
        Returns empty list because this is a pure functional plugin with no AI Agents.
        """
        return []

    # ── §2.3 Dashboard stats ──
    def get_dashboard_stats(self):
        """Return dashboard statistics. Pure login plugin — no metrics exposed."""
        return []

    # ── §10.6 Schema version & migration ──
    def get_schema_version(self):
        """Return current schema version for migration tracking."""
        return 1

    def migrate(self, from_version, to_version):
        """Run schema migrations between versions.
        Currently placeholder — no pending migrations.
        """
        if from_version >= to_version:
            return True
        _plugin_log(f'[OauthConfigPlugin] No migrations needed (v{from_version} → v{to_version})')
        return True

    def on_enable(self, registry):
        init_i18n(self.t)
        # 自动初始化插件独立数据库
        from .models import init_oauth_tables
        init_oauth_tables()
        _plugin_log('[OauthConfigPlugin] OAuth login & configuration plugin enabled')
        return True

    def register_routes(self):
        """注册后台配置路由（首次加载时自动初始化独立数据库）"""
        from .models import init_oauth_tables
        init_oauth_tables()
        from .routes.admin import oauth_cfg_bp
        return [oauth_cfg_bp]

    def on_disable(self, registry):
        _plugin_log('[OauthConfigPlugin] OAuth plugin disabled', 'warning')
        return True

    # ── §12.5 Uninstall cleanup ──
    def on_uninstall(self):
        """Clean up main-DB oauth_providers table on plugin uninstall (§12.5 卸载零残留)."""
        try:
            from .models import get_db
            with get_db() as conn:
                conn.execute('DROP TABLE IF EXISTS oauth_providers')
                conn.commit()
            _plugin_log('[OauthConfigPlugin] oauth_providers table dropped (main DB, uninstall)')
        except Exception as e:
            _plugin_log(f'[OauthConfigPlugin] on_uninstall cleanup failed: {e}', 'error')
        return True

    # ── Login method registration (dynamic UI) ──

    def get_login_methods(self):
        """Register all enabled OAuth providers as third-party login methods."""
        from .services.oauth_service import get_enabled_oauth_providers
        providers = get_enabled_oauth_providers()
        return [
            {
                'type': 'oauth',
                'provider': p['provider'],
                'name': p['name'],
                'icon': p['provider'],
                'priority': 30 + i,
                'login_url': p['login_url'],
                'is_third_party': True,
            }
            for i, p in enumerate(providers)
        ]
