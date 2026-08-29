#!/usr/bin/env python3
"""
Payment Gateway — Stripe 支付网关
=======================================
国际区 (DEPLOY_MARKET=intl) 默认支付渠道。
接口: Stripe Checkout Session
"""

import os
import json
from typing import Dict, Any, Tuple


def _get_stripe_config() -> dict:
    cfg = {
        'secret_key': os.environ.get('STRIPE_SECRET_KEY', ''),
        'publishable_key': os.environ.get('STRIPE_PUBLISHABLE_KEY', ''),
        'webhook_secret': os.environ.get('STRIPE_WEBHOOK_SECRET', ''),
    }

    # H-03：环境变量缺失时从 system_config 表读取兜底
    if not cfg['secret_key']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'secret_key': 'stripe_secret_key',
            'publishable_key': 'stripe_publishable_key',
            'webhook_secret': 'stripe_webhook_secret',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def create_stripe_session(order_no: str, amount_fen: int, subject: str,
                          description: str, interval_type: str = 'month') -> Dict[str, Any]:
    """创建 Stripe Checkout Session

    Returns:
        Dict with redirect_url for client redirect.
    """
    from . import _is_placeholder
    cfg = _get_stripe_config()
    sk = cfg['secret_key']

    if not sk or _is_placeholder(sk):
        # C-02：未配置不再返回 mock 跳转，避免产生无法支付的 pending 订单
        print('[Stripe] Not configured, cannot create payment')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': 'Stripe gateway not configured',
        }

    try:
        import stripe
        stripe.api_key = sk

        # 构建 price name
        interval_str = 'Monthly' if interval_type == 'month' else 'Yearly'

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{subject} ({interval_str})',
                        'description': description,
                    },
                    'unit_amount': amount_fen,  # Stripe 用 smallest currency unit (cents)
                    # H1: 移除 'recurring' 块，避免创建真实自动扣款的 Stripe 订阅
                },
                'quantity': 1,
            }],
            mode='payment',  # H1: 改为一次性支付；续费由平台新建订单 + 新 Session 完成，不再并行扣款
            success_url=os.environ.get('SUCCESS_URL', '/subscribe/success?session_id={CHECKOUT_SESSION_ID}'),
            cancel_url=os.environ.get('CANCEL_URL', '/subscribe/cancel'),
            metadata={
                'order_no': order_no,
            },
        )

        return {
            'success': True,
            'trade_no': session.id,
            'qr_code': '',
            'redirect_url': session.url or '',
        }

    except ImportError:
        # C-02：未安装 SDK 不再返回 mock，改为明确失败
        print('[Stripe] stripe-python not installed, cannot create payment')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': 'stripe-python SDK not installed',
        }
    except Exception as e:
        print(f'[Stripe] Error: {e}')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': str(e),
        }


def refund_stripe_session(trade_no: str, amount_fen: int = 0) -> Dict[str, Any]:
    """Stripe 退款

    Args:
        trade_no: Stripe PaymentIntent ID 或 Checkout Session ID
        amount_fen: 退款金额（cents），0 表示全额退款

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    from . import _is_placeholder
    cfg = _get_stripe_config()
    sk = cfg['secret_key']

    if not sk or _is_placeholder(sk):
        # C-01：未配置不再返回 mock 退款成功，否则订单被标记 refunded 但资金未退回
        print('[Stripe Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False,
            'refund_no': '',
            'error': 'Stripe gateway not configured; refund requires manual processing',
        }

    try:
        import stripe
        stripe.api_key = sk

        # 如果是 Checkout Session ID (cs_xxx)，先获取 PaymentIntent
        pi_id = trade_no
        if trade_no.startswith('cs_'):
            session = stripe.checkout.Session.retrieve(trade_no)
            pi_id = session.payment_intent or trade_no

        refund_params = {'payment_intent': pi_id}
        if amount_fen > 0:
            refund_params['amount'] = amount_fen

        refund = stripe.Refund.create(**refund_params)

        return {'success': True, 'refund_no': refund.id, 'error': ''}

    except ImportError:
        # C-01：SDK 未安装时拒绝退款，避免虚假成功（与 create_stripe_session 一致）
        print('[Stripe Refund] stripe-python not installed, refund rejected')
        return {
            'success': False,
            'refund_no': '',
            'error': 'stripe-python SDK not installed; refund requires manual processing',
        }
    except Exception as e:
        print(f'[Stripe Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


def verify_stripe_webhook(raw_data: dict, headers: dict) -> Tuple[bool, dict]:
    """验证 Stripe Webhook 事件

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    cfg = _get_stripe_config()
    webhook_secret = cfg['webhook_secret']

    from . import _is_gateway_configured
    if not _is_gateway_configured('stripe'):
        # ❌ 旧代码：Mock mode 直接返回 True（认证绕过风险），此处拒绝所有回调
        print('[Stripe] SECURITY: webhook secret not configured, rejecting webhook')
        return False, {}

    try:
        import stripe
        stripe.api_key = cfg['secret_key']

        # 从请求体获取原始 payload（调用方需要传入 payload 字符串）
        payload = raw_data.get('_raw_payload', '')
        sig_header = headers.get('Stripe-Signature', '')

        if not payload or not sig_header:
            return False, {}

        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)

        if event.type == 'checkout.session.completed':
            session = event.data.object
            return True, {
                'order_no': session.get('metadata', {}).get('order_no', ''),
                'trade_no': session.get('id', ''),
                'status': 'paid',
                # VR-PAY-002：amount_total 单位为分（cents），复用 total_fee 键喂给 M1 金额校验（1 分容差）
                'total_fee': session.get('amount_total') or 0,
            }

        return False, {}

    except ImportError:
        # ❌ 旧代码：未安装 stripe-python 时也返回 True，此处拒绝并告警
        print('[Stripe] SECURITY: stripe-python not installed, rejecting webhook')
        return False, {}
    except Exception as e:
        print(f'[Stripe] Webhook verify error: {e}')
        return False, {}


# ═══════════════════════════════════════════════════════════════════════════
# 兼容别名（V20260812 合流）
# 商城 shop/admin.py 依赖 A 的签名 refund_order(trade_no, amount_fen=0)
# Phase 2 重接线 shop 后仅需改 import 路径，函数签名保持不变。
# ═══════════════════════════════════════════════════════════════════════════
refund_order = refund_stripe_session
