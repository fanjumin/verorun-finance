"""订单通知插件 — 基于事件系统的自动通知"""
from plugin_manager.base import BasePlugin
from plugin_manager.event_bus import EventName, get_event_bus
from plugin_manager.logger import get_plugin_logger
from .models import get_main_db, init_db

logger = get_plugin_logger('order_notify')


class OrderNotifyPlugin(BasePlugin):
    name = 'order_notify'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Order Notify — Auto send notifications on order created, paid, shipped, refunded'

    def on_enable(self, registry) -> bool:
        try:
            init_db()
            bus = get_event_bus()
            bus.on(EventName.ORDER_CREATED, self._on_created)
            bus.on(EventName.ORDER_PAID, self._on_paid)
            bus.on(EventName.ORDER_SHIPPED, self._on_shipped)
            bus.on(EventName.ORDER_REFUNDED, self._on_refunded)
            bus.on(EventName.ORDER_CANCELLED, self._on_cancelled)
            bus.on(EventName.ORDER_COMPLETED, self._on_completed)
            return True
        except Exception as e:
            logger.error(f"Failed to enable order_notify: {e}")
            return False

    def on_disable(self, registry) -> bool:
        bus = get_event_bus()
        bus.off(EventName.ORDER_CREATED, self._on_created)
        bus.off(EventName.ORDER_PAID, self._on_paid)
        bus.off(EventName.ORDER_SHIPPED, self._on_shipped)
        bus.off(EventName.ORDER_REFUNDED, self._on_refunded)
        bus.off(EventName.ORDER_CANCELLED, self._on_cancelled)
        bus.off(EventName.ORDER_COMPLETED, self._on_completed)
        return True

    # ── 通知辅助 ──

    def _notify_user(self, user_id: int, title: str, content: str, link: str = ''):
        """发送站内通知"""
        try:
            from notification_service import send_notification_by_event
            # 兼容不同导入路径
            send_notification_by_event('system', user_id, {
                'title': title,
                'content': content,
                'link_url': link,
            })
        except Exception as e:
            logger.warning(f"[OrderNotify] 发送通知失败: {e}")

    # ── 事件处理 ──

    def _on_created(self, **kw):
        """下单通知"""
        uid = kw.get('user_id')
        oid = kw.get('order_id')
        total = kw.get('total', 0)
        if uid:
            self._notify_user(
                uid,
                self.t('Order has been created'),
                self.t('Your order {order_id} has been created ({amount:.2f}). Please complete payment soon.').format(order_id=oid, amount=total),
                f'/mall/orders'
            )

    def _on_paid(self, **kw):
        """支付成功通知"""
        oid = kw.get('order_id')
        uid = kw.get('user_id', 0)
        # 事件参数里可能没有 user_id，从数据库查
        if not uid:
            try:
                with get_main_db() as conn:
                    row = conn.execute(
                        'SELECT user_id FROM order_items WHERE order_id=%s LIMIT 1', (oid,)
                    ).fetchone()
                    if row:
                        uid = row['user_id']
            except Exception:
                pass
        if uid:
            self._notify_user(
                uid,
                self.t('Payment successful'),
                self.t('Your order {order_id} has been successfully paid. We will ship it to you as soon as possible!').format(order_id=oid),
                f'/mall/orders'
            )

    def _on_shipped(self, **kw):
        """发货通知"""
        uid = kw.get('user_id')
        oid = kw.get('order_id')
        company = kw.get('company', '')
        tracking = kw.get('tracking_number', '')
        if uid:
            msg = self.t('Your order {order_id} has been shipped!').format(order_id=oid)
            if company and tracking:
                msg += '\n' + self.t('Courier: {company} | Tracking: {tracking}').format(company=company, tracking=tracking)
            self._notify_user(uid, self.t('Shipped'), msg, f'/mall/orders')

    def _on_refunded(self, **kw):
        """退款通知"""
        uid = kw.get('user_id')
        oid = kw.get('order_id')
        reason = kw.get('reason', '')
        if uid:
            msg = self.t('Your refund request for order {order_id} has been received').format(order_id=oid)
            if reason:
                msg += '\n' + self.t('Reason: {reason}').format(reason=reason)
            self._notify_user(uid, self.t('Refund Requested'), msg, f'/mall/orders')

    def _on_cancelled(self, **kw):
        """取消通知"""
        uid = kw.get('user_id')
        oid = kw.get('order_id')
        if uid:
            self._notify_user(uid, self.t('Order canceled'),
                              self.t('Your order {order_id} has been canceled.').format(order_id=oid), f'/mall/orders')

    def _on_completed(self, **kw):
        """完成通知"""
        uid = kw.get('user_id')
        oid = kw.get('order_id')
        if uid:
            self._notify_user(uid, self.t('Order completed'),
                              self.t('Your order {order_id} is completed. Welcome back! Please leave a review.').format(order_id=oid),
                              f'/mall/orders')

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读插件独立 schema order_notify，幂等）。"""
        stats = {'total_notifications': 0, 'today_notifications': 0}
        try:
            from .models import get_db
            with get_db() as conn:
                conn.execute('SET search_path TO order_notify')
                total = conn.execute('SELECT COUNT(*) AS c FROM notification_log').fetchone()
                today = conn.execute(
                    'SELECT COUNT(*) AS c FROM notification_log '
                    'WHERE created_at::timestamptz>=CURRENT_DATE'
                ).fetchone()
                stats['total_notifications'] = int(total['c']) if total else 0
                stats['today_notifications'] = int(today['c']) if today else 0
        except Exception as e:
            logger.error(f'get_dashboard_stats failed: {e}')
        return stats

    def on_uninstall(self, registry) -> bool:
        """F-010: 卸载清理 — 删除 order_notify schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS order_notify CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('order_notify schema dropped')
        except Exception as e:
            logger.error(f'on_uninstall cleanup failed: {e}')
        return True
