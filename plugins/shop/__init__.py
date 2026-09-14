#!/usr/bin/env python3
"""
Shop/Mall Plugin — 商城核心插件
================================
Product management, categories, orders, cart, checkout.
Decoupled from core system as a standalone plugin.
"""
from i18n import _
import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
if os.path.isdir(_ROOT) and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('shop')


class ShopPlugin(BasePlugin):
    """ShopPlugin — 元数据以 plugin.json（plugin_info）为唯一数据源，类内仅保留兜底值。"""

    @property
    def name(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'identifier', None) or 'shop'

    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'

    @property
    def description(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'description', None) or 'Shop/Mall — Product & Order Management'

    @property
    def author(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'author', None) or 'VeroRun'

    def on_install(self, registry):
        self._init_db()
        return True

    def on_enable(self, registry):
        self._init_db()
        logger.info('[ShopPlugin] ✅ Shop plugin enabled')
        return True

    def _init_db(self):
        from .models.database import init_shop_db
        init_shop_db()

    def register_routes(self):
        from .routes.admin import shop_admin_bp
        from .routes.public import shop_public_bp
        logger.info('[ShopPlugin] ✅ /mall/* routes registered')
        return [shop_admin_bp, shop_public_bp]

    def on_disable(self, registry):
        logger.info('[ShopPlugin] ⚠️ Shop plugin disabled')
        return True

    def register_jobs(self):
        """P2：弃单挽回定时任务（由系统 APScheduler 调度）"""
        from .scheduler import SHOP_JOBS
        return SHOP_JOBS

    def get_event_handlers(self):
        """订阅 wishlist 收藏事件，更新商品收藏热度（wish_count）供推荐排序"""
        from .scheduler import on_wishlist_updated
        return {'wishlist.updated': on_wishlist_updated}

    def get_dashboard_stats(self) -> dict:
        """返回 Dashboard 统计指标（§2.3/§10.5）"""
        try:
            from .models import get_db
            from datetime import date
            today = date.today().isoformat()
            with get_db() as conn:
                total_products = conn.execute(
                    'SELECT COUNT(*) FROM products WHERE is_active=1'
                ).fetchone()[0]
                total_orders = conn.execute(
                    'SELECT COUNT(*) FROM order_items'
                ).fetchone()[0]
                orders_today = conn.execute(
                    "SELECT COUNT(*) FROM order_items WHERE created_at::date=%s",
                    (today,)
                ).fetchone()[0]
                revenue_today = conn.execute(
                    "SELECT COALESCE(SUM(subtotal - discount), 0) FROM order_items WHERE status='paid' AND paid_at::date=%s",
                    (today,)
                ).fetchone()[0]
            return {
                'total_products': total_products,
                'total_orders': total_orders,
                'orders_today': orders_today,
                'revenue_today': round(float(revenue_today), 2),
            }
        except Exception as e:
            logger.error(f'get_dashboard_stats failed: {e}')
            return {}

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 shop schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS shop CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('[ShopPlugin] shop schema dropped')
        except Exception as e:
            logger.error(f'[ShopPlugin] on_uninstall cleanup failed: {e}')
        return True
