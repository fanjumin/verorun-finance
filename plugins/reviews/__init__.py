"""商品评价系统插件"""
from i18n import _
import logging
from typing import List

from plugin_manager.base import BasePlugin
from plugin_manager.event_bus import EventName, get_event_bus
from .models import get_db, get_main_db, init_db

logger = logging.getLogger(__name__)


class ReviewsPlugin(BasePlugin):
    name = 'reviews'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = _('Product Reviews — rate, review, and share photos of purchased products')

    def on_install(self, registry) -> bool:
        """安装时创建插件表"""
        init_db()
        return True

    def on_enable(self, registry) -> bool:
        """订阅事件：支付成功后提示评价"""
        bus = get_event_bus()
        bus.on(EventName.ORDER_PAID, self._on_order_paid)
        return True

    def on_disable(self, registry) -> bool:
        bus = get_event_bus()
        bus.off(EventName.ORDER_PAID, self._on_order_paid)
        return True

    def on_uninstall(self, registry) -> bool:
        """F-010: 卸载清理 — 删除 reviews schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS reviews CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('reviews schema dropped')
        except Exception as e:
            logger.error(f'on_uninstall cleanup failed: {e}')
        return True

    def _on_order_paid(self, **kwargs):
        """支付成功后记录——用户可在订单页写评价"""
        logger.info(f"[Reviews] 订单 {kwargs.get('order_id')} 已支付，可评价")

    def register_routes(self) -> List:
        from .routes import init_routes, reviews_bp
        init_routes(get_db, get_main_db, self.t)
        return [reviews_bp]

    def get_dashboard_stats(self) -> dict:
        """返回 Dashboard 统计指标（从 reviews schema 汇总，异常时返回零值）。"""
        try:
            with get_db() as conn:
                row = conn.execute('''
                    SELECT
                        COUNT(*) AS total_reviews,
                        COALESCE(ROUND(AVG(rating)::numeric, 1), 0) AS avg_rating,
                        COALESCE(ROUND(100.0 * COUNT(*) FILTER (WHERE rating >= 4) / NULLIF(COUNT(*), 0), 1), 0) AS positive_rate,
                        COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS reviews_24h,
                        COUNT(*) FILTER (WHERE images IS NOT NULL AND images <> '' AND images <> '[]') AS with_images
                    FROM product_reviews
                    WHERE is_active = 1
                ''').fetchone()
            return {
                'total_reviews': row['total_reviews'],
                'avg_rating': float(row['avg_rating']),
                'positive_rate': float(row['positive_rate']),
                'reviews_24h': row['reviews_24h'],
                'with_images': row['with_images'],
            }
        except Exception as e:
            logger.error(f"get_dashboard_stats error: {e}")
            return {'total_reviews': 0, 'avg_rating': 0, 'positive_rate': 0, 'reviews_24h': 0, 'with_images': 0}
