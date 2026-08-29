#!/usr/bin/env python3
"""
Subscription Plugin — 统一按需订阅管理
=========================================
按 Feature/SKU 独立订阅，废弃套餐制。
支持双环境支付路由：
  - verorun.cn  → Alipay / WeChat Pay
  - verorun.com → Stripe / PayPal

使用方式:
    from plugins.subscription.services import has_subscription, SubscriptionService
    from plugins.subscription.routes import sub_bp
"""

from plugin_manager.base import BasePlugin


class SubscriptionPlugin(BasePlugin):
    name = 'subscription'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Pay-as-you-go subscription marketplace — per-item billing with dual-environment payments'
    author = 'VeroRun'

    # L-04: 配置 Schema（与 plugin.json settings_schema 对齐）
    config_schema = {
        'trial_days': {'type': 'integer', 'default': 0, 'minimum': 0},
        'grace_days': {'type': 'integer', 'default': 3, 'minimum': 0},
        'auto_renew_default': {'type': 'boolean', 'default': True},
    }

    def get_menu(self):
        """动态注册管理菜单：插件启动后自动挂载到 BUSINESS CENTER > Subscription"""
        return {
            'group': 'Business Center',
            'items': [
                {'key': 'subscription_admin', 'icon': 'credit_card', 'label': 'Subscription'},
            ],
        }

    def on_install(self, registry):
        """安装时创建独立数据库表 + 种子 SKU 目录"""
        from .models import init_tables, seed_default_items
        try:
            init_tables()
            seed_default_items()
            print('[Subscription] DB tables created and seeded')
        except Exception as e:
            print(f'[Subscription] DB init error: {e}')
            return False
        return True

    def on_enable(self, registry):
        """启用时: 确保表存在 + 种子 + 初始化 i18n"""
        from .models import init_tables, seed_default_items
        init_tables()
        seed_default_items()

        # 初始化插件 i18n
        from . import routes as _routes
        from . import services as _services
        _routes.init_i18n(self.t)
        _services.init_i18n(self.t)
        print('[Subscription] Plugin i18n initialized')

        return True

    def register_routes(self):
        """注册订阅 Blueprint"""
        from .routes import sub_bp
        return [sub_bp]

    def register_jobs(self):
        """注册 APScheduler 定时任务"""
        from .scheduler import SUBSCRIPTION_JOBS
        return SUBSCRIPTION_JOBS

    def get_event_handlers(self):
        """暴露订阅能力为事件处理器（供其他模块/消费端调用）

        Provides: has / list / subscribe / cancel / renew / check_module_access / site_plans
        """
        from .services import get_subscription_service
        svc = get_subscription_service()
        return {
            'subscription/has': lambda user_id, item_key: svc.has_subscription(user_id, item_key),
            'subscription/list': lambda locale='zh-CN': svc.list_items(locale),
            'subscription/subscribe': lambda user_id, item_key, interval_type, channel=None: svc.subscribe(
                user_id, item_key, interval_type, channel),
            'subscription/cancel': lambda user_id, item_key, immediate=False: svc.cancel(
                user_id, item_key, immediate),
            'subscription/renew': lambda user_id, item_key, channel=None: svc.renew(
                user_id, item_key, channel),
            'subscription/check_module_access': lambda user_id, module_key: svc.check_module_access(
                user_id, module_key),
            'subscription/site_plans': lambda active_only=True: svc.list_site_plans(active_only),
        }

    def on_disable(self, registry):
        print('[Subscription] Disabled')
        return True

    def on_uninstall(self, registry):
        """卸载时清理插件独立 schema（H-05）

        仅删除 subscription schema 中的插件表，不动主库公共数据。
        返回 False 表示卸载失败，PluginManager 将中止卸载。
        """
        from plugins._base.db import get_raw_connection, PgConnection
        try:
            conn = PgConnection(get_raw_connection())
            try:
                conn.execute("DROP SCHEMA IF EXISTS subscription CASCADE")
                conn.commit()
                print('[Subscription] Schema dropped')
            finally:
                conn.close()
        except Exception as e:
            print(f'[Subscription] Uninstall error: {e}')
            return False
        return True

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读 subscription 独立 schema，幂等）。"""
        from plugins._base.db import get_raw_connection, PgConnection
        stats = {'active_subscriptions': 0, 'total_plans': 0, 'total_revenue_fen': 0}
        try:
            conn = PgConnection(get_raw_connection())
            try:
                conn.execute('SET search_path TO subscription')
                active = conn.execute(
                    "SELECT COUNT(*) AS c FROM user_subscriptions WHERE status='active'"
                ).fetchone()
                plans = conn.execute(
                    'SELECT COUNT(*) AS c FROM sub_items WHERE is_active=1'
                ).fetchone()
                rev = conn.execute(
                    "SELECT COALESCE(SUM(amount_fen),0) AS c FROM sub_orders WHERE status='paid'"
                ).fetchone()
                stats['active_subscriptions'] = int(active['c']) if active else 0
                stats['total_plans'] = int(plans['c']) if plans else 0
                stats['total_revenue_fen'] = int(rev['c']) if rev else 0
            finally:
                conn.close()
        except Exception as e:
            print(f'[Subscription] get_dashboard_stats error: {e}')
        return stats
