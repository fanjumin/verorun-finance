#!/usr/bin/env python3
"""Shop Scheduler — P2 弃单挽回：扫描超时未支付订单并发站内信提醒"""
from .models import get_db
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('shop')

# 弃单超时分钟数（下单后 X 分钟仍未支付视为弃单）
ABANDON_TIMEOUT_MINUTES = 30


def _get_default_lang():
    """读取站点默认语言（system_config.default_language），决定通知文案语言"""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key='default_language'"
            ).fetchone()
            return (row['value'] if row else 'zh-CN') or 'zh-CN'
    except Exception:
        return 'zh-CN'


def scan_abandoned_orders():
    """弃单挽回：扫描 pending 超时订单 → 站内信提醒 → 标记已提醒（幂等，PG advisory lock 防并发）"""
    from plugins._base.db import get_raw_connection
    raw = None
    try:
        raw = get_raw_connection()
        cur = raw.cursor()
        # 'SHOP' 的十六进制，作为本任务 advisory lock key，防多 worker 重复扫描
        cur.execute('SELECT pg_try_advisory_lock(%s)', (0x53484F50,))
        if not cur.fetchone()[0]:
            return
        try:
            from services.notification_service import create_notification
            zh = _get_default_lang() == 'zh-CN'
            with get_db() as conn:
                rows = conn.execute(
                    '''SELECT DISTINCT oi.id AS oid, oi.user_id, oi.order_id
                       FROM shop.order_items oi
                       WHERE oi.status='pending'
                         AND oi.abandon_reminded_at IS NULL
                         AND oi.created_at < NOW() - (%s || ' minutes')::interval''',
                    (ABANDON_TIMEOUT_MINUTES,)
                ).fetchall()
                for r in rows:
                    title = '未支付订单提醒' if zh else 'Unpaid Order Reminder'
                    content = (
                        f"订单 {r['order_id']} 仍在等待支付，请尽快完成支付以确认订单。"
                        if zh else
                        f"Order {r['order_id']} is awaiting payment. Please complete your payment to confirm your order."
                    )
                    create_notification(
                        r['user_id'], 'shop_abandon_cart', title, content,
                        link_url=f"/mall/pay/{r['oid']}"
                    )
                    conn.execute(
                        'UPDATE shop.order_items SET abandon_reminded_at=NOW() WHERE id=%s',
                        (r['oid'],)
                    )
                conn.commit()
        finally:
            cur.execute('SELECT pg_advisory_unlock(%s)', (0x53484F50,))
            cur.close()
    finally:
        if raw is not None:
            raw.close()


SHOP_JOBS = [{
    'job_id': 'shop_abandoned_cart_scan',
    'func': scan_abandoned_orders,
    'trigger': 'interval',
    'kwargs': {'minutes': ABANDON_TIMEOUT_MINUTES},
    'priority': 3,
    'max_retries': 2,
    'description': 'Shop abandoned cart recovery scan',
}]


def on_wishlist_updated(**kw):
    """wishlist.updated 事件处理：更新商品收藏热度（wish_count），供推荐排序

    由 EventBus 异步线程调用（无 request context），使用独立 DB 连接。
    """
    action = kw.get('action')
    product_id = kw.get('product_id')
    if action not in ('added', 'removed') or not product_id:
        return
    delta = 1 if action == 'added' else -1
    try:
        with get_db() as conn:
            conn.execute(
                'UPDATE shop.products SET wish_count = GREATEST(0, wish_count + %s) WHERE id=%s',
                (delta, product_id)
            )
            conn.commit()
    except Exception as e:
        logger.error(f'[ShopPlugin] wishlist event handler error: {e}')
