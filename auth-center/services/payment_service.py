#!/usr/bin/env python3
"""
Shop Payment Service — 商城支付服务
商城订单支付逻辑（支付宝/微信均委托 gateway 实现，此处仅封装 shop 专用流程）

配置优先级：
1. system_config 表（alipay_app_id / alipay_private_key / alipay_public_key）
2. 环境变量（ALIPAY_APP_ID / NOTIFY_BASE）
3. 未配置 → fail-closed 返回失败（不再 mock 假成功，VR-PAY-003 修复）
"""

import os, sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _resolve_notify_base():
    """获取通知域名：环境变量 → deploy 配置"""
    notify_base = os.environ.get('NOTIFY_BASE', '')
    if notify_base:
        return notify_base
    try:
        from services.deployment_config import deploy
        return deploy.url()
    except Exception:
        return ''


def create_shop_payment(order_id: str, total_amount: float, subject: str = '商城订单') -> dict:
    """
    为商城订单创建支付宝扫码支付，委托 plugins/subscription/gateways/alipay.py

    Returns:
        {'success': bool, 'qr_code': str, 'order_id': str, 'amount': str, ...}
        未配置时返回失败（fail-closed，不再 mock 假成功）
    """
    from shared.plugin_access import get_attr
    create_alipay_order = get_attr('plugins.payment.gateways.alipay', 'create_alipay_order',
                                   feature='shop_payment_create')
    if create_alipay_order is None:
        return {
            'success': False,
            'qr_code': '',
            'pay_url': '',
            'order_id': order_id,
            'error': 'Payment gateway unavailable (plugins/payment not installed)',
        }

    amount_fen = int(round(total_amount * 100))
    notify_base = _resolve_notify_base()
    shop_notify_url = f'{notify_base}/mall/api/pay/notify' if notify_base else ''

    result = create_alipay_order(order_id, amount_fen, subject, subject,
                                 notify_url=shop_notify_url)

    if not result.get('success'):
        return {
            'success': False,
            'qr_code': '',
            'pay_url': '',
            'order_id': order_id,
            'error': result.get('error', '支付宝网关未配置'),
        }

    return {
        'success': True,
        'qr_code': result.get('qr_code', ''),
        'pay_url': '',
        'order_id': order_id,
        'amount': f'{amount_fen/100:.2f}',
        'note': '请使用支付宝扫码完成支付',
    }


def verify_notify(data: dict) -> bool:
    """验证支付宝异步通知签名，委托 plugins/subscription/gateways/alipay.py"""
    from shared.plugin_access import get_attr
    verify_alipay_notify = get_attr('plugins.payment.gateways.alipay', 'verify_alipay_notify',
                                    feature='shop_payment_verify')
    if verify_alipay_notify is None:
        return False
    is_valid, _ = verify_alipay_notify(data, {})
    return is_valid


def confirm_shop_order(order_id: str, trade_no: str = '', payment_method: str = 'alipay'):
    """
    确认商城订单已支付
    - 更新 order_items.status = paid
    - 记录支付信息
    - 创建 user_purchases
    """
    from models import get_db
    now = datetime.now().isoformat()
    with get_db() as conn:
        items = conn.execute(
            'SELECT * FROM order_items WHERE order_id=%s AND status=\'pending\'',
            (order_id,)
        ).fetchall()
        if not items:
            return False, '订单不存在或已支付'

        for item in items:
            conn.execute(
                '''UPDATE order_items SET status='paid', paid_at=%s, payment_method=%s,
                   payment_trade_no=%s WHERE id=%s''',
                (now, payment_method, trade_no, item['id'])
            )
            # 创建购买记录
            existing = conn.execute(
                'SELECT id FROM user_purchases WHERE user_id=%s AND product_id=%s AND order_id=%s',
                (item['user_id'], item['product_id'], order_id)
            ).fetchone()
            if not existing:
                conn.execute(
                    '''INSERT INTO user_purchases (user_id, product_id, order_id,
                       purchase_type, status, created_at)
                       VALUES (%s,%s,%s,'once','active',%s)''',
                    (item['user_id'], item['product_id'], order_id, now)
                )
        conn.commit()
    
    # 触发事件：订单支付成功
    try:
        from plugin_manager.event_bus import get_event_bus, EventName
        get_event_bus().emit(EventName.ORDER_PAID, order_id=order_id, trade_no=trade_no,
                             payment_method=payment_method)
    except Exception:
        pass
    
    return True, '支付成功'
