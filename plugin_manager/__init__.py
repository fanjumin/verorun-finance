#!/usr/bin/env python3
"""
Plugin Manager — 插件管理核心模块
====================================
插件生命周期管理、发现、依赖解析、钩子系统集成。

使用方式:
    manager = PluginManager()
    manager.init_app(app)
    manager.discover()
    manager.install('coupons')
    manager.enable('coupons')
"""

from .manager import PluginManager
from .models import PluginStatus, PluginInfo, init_plugin_registry_table, get_registry_db
from .hooks import HookRegistry, get_hook_registry
from .deps import topological_sort, build_dependency_graph
from .config_validator import validate_config, coerce_config
from .logger import get_plugin_logger, init_plugin_logging, read_plugin_log
from .license import LicenseManager, get_license_manager
from .store import StoreAPIClient, get_store_client
from .payment import (
    get_payment_router, PaymentRouter, PaymentProvider,
    PaymentOrder, OrderStatus, create_payment_order,
    get_payment_order, update_payment_order,
    AlipayProvider, WechatProvider, StripeProvider, PayPalProvider, MockProvider,
)
from .subscription import (
    get_subscription_manager, SubscriptionManager, PluginSubscription,
)
from .models_store import LicenseRecord, LicenseType, LicenseStatus, StorePlugin, PluginReview
from .coupons import CouponManager, get_coupon_manager
from .license import submit_plugin

__all__ = [
    'PluginManager',
    'PluginStatus',
    'PluginInfo',
    'HookRegistry',
    'get_hook_registry',
    'topological_sort',
    'build_dependency_graph',
    'validate_config',
    'coerce_config',
    'get_plugin_logger',
    'init_plugin_logging',
    'read_plugin_log',
    'LicenseManager',
    'get_license_manager',
    'StoreAPIClient',
    'get_store_client',
    'LicenseRecord',
    'LicenseType',
    'LicenseStatus',
    'StorePlugin',
    'get_payment_router',
    'PaymentRouter',
    'PaymentProvider',
    'PaymentOrder',
    'OrderStatus',
    'create_payment_order',
    'get_payment_order',
    'update_payment_order',
    'AlipayProvider',
    'WechatProvider',
    'StripeProvider',
    'PayPalProvider',
    'MockProvider',
    'get_subscription_manager',
    'SubscriptionManager',
    'PluginSubscription',
    'CouponManager',
    'get_coupon_manager',
    'PluginReview',
    'submit_plugin',
    'init_plugin_registry_table',
    'get_registry_db',
]
