#!/usr/bin/env python3
"""
Payment Gateway — 支付网关入口
====================================
根据 DEPLOY_MARKET 自动路由支付渠道:
  - cn   → Alipay / WeChat Pay / Airwallex / LianLian
  - intl → Stripe / PayPal / Adyen / Checkout.com
"""

import os
from typing import Dict, Any, Tuple

# L-03：占位符模式检测（用于判断配置是否仍为占位值，而非真实密钥）
_PLACEHOLDER_PATTERNS = ('xxx', 'your-', 'placeholder', 'change_me')


def _is_placeholder(value: str) -> bool:
    """判断配置值是否为占位符（空值/含常见占位标记）"""
    v = (value or '').lower().strip()
    if not v:
        return True
    return any(p in v for p in _PLACEHOLDER_PATTERNS)


def _get_config_from_db(key_map: Dict[str, str]) -> Dict[str, str]:
    """从主库 system_config 表读取网关配置（H-03 DB 兜底）

    Args:
        key_map: {config 字段名: system_config 键名}

    Returns:
        仅返回 DB 中存在的键值对；异常时返回空 dict（不阻断调用方）。
    """
    try:
        from plugins._base.db import get_raw_connection
        import psycopg2
        import psycopg2.extras
        conn = get_raw_connection()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            db_keys = list(key_map.values())
            placeholders = ','.join(['%s'] * len(db_keys))
            cur.execute(
                f"SELECT key, value FROM system_config WHERE key IN ({placeholders})",
                db_keys,
            )
            rows = cur.fetchall()
            cur.close()
            return {r['key']: r['value'] for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def create_payment(order_no: str, amount_fen: int, subject: str, description: str,
                   channel: str = 'alipay', interval_type: str = 'month') -> Dict[str, Any]:
    """统一支付入口：根据 channel 路由到对应网关

    Returns:
        Dict with keys: qr_code, redirect_url, trade_no, success
    """
    market = os.environ.get('DEPLOY_MARKET', 'cn')

    if channel == 'alipay' and market == 'cn':
        from .alipay import create_alipay_order
        return create_alipay_order(order_no, amount_fen, subject, description)

    elif channel == 'wechat' and market == 'cn':
        from .wechat import create_wechat_order
        return create_wechat_order(order_no, amount_fen, subject, description, interval_type)

    elif channel == 'stripe' and market == 'intl':
        from .stripe import create_stripe_session
        return create_stripe_session(order_no, amount_fen, subject, description, interval_type)

    elif channel == 'paypal' and market == 'intl':
        from .paypal import create_paypal_order
        return create_paypal_order(order_no, amount_fen, subject, description, interval_type)

    elif channel == 'adyen' and market == 'intl':
        from .adyen import create_adyen_session
        return create_adyen_session(order_no, amount_fen, subject, description)

    elif channel == 'checkoutcom' and market == 'intl':
        from .checkoutcom import create_checkout_session
        return create_checkout_session(order_no, amount_fen, subject, description)

    elif channel == 'airwallex' and market == 'cn':
        from .airwallex import create_airwallex_intent
        return create_airwallex_intent(order_no, amount_fen, subject, description)

    elif channel == 'lianlian' and market == 'cn':
        from .lianlian import create_lianlian_order
        return create_lianlian_order(order_no, amount_fen, subject, description)

    else:
        # C-02：未知渠道不再返回 mock 成功，避免产生无法支付的 pending 订单
        print(f'[Payment] Unknown payment channel: {channel}')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': f'Unknown payment channel: {channel}',
        }


def verify_notify(channel: str, raw_data: dict, headers: dict = None,
                  raw_body: bytes = None) -> tuple:
    """统一支付回调验证

    Args:
        channel: alipay | wechat | stripe | paypal | adyen | checkoutcom | airwallex | lianlian
        raw_data: 已解析的回调数据（dict）
        headers: 请求头
        raw_body: 原始请求体（PayPal/Adyen/Checkout.com/Airwallex webhook 验签必需）

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    if channel == 'alipay':
        from .alipay import verify_alipay_notify
        return verify_alipay_notify(raw_data, headers or {})

    elif channel == 'wechat':
        from .wechat import verify_wechat_notify
        return verify_wechat_notify(raw_data, headers or {})

    elif channel == 'stripe':
        from .stripe import verify_stripe_webhook
        return verify_stripe_webhook(raw_data, headers or {})

    elif channel == 'paypal':
        from .paypal import verify_paypal_webhook
        return verify_paypal_webhook(raw_body, raw_data, headers or {})

    elif channel == 'adyen':
        from .adyen import verify_adyen_webhook
        return verify_adyen_webhook(raw_body, headers or {})

    elif channel == 'checkoutcom':
        from .checkoutcom import verify_checkout_webhook
        return verify_checkout_webhook(raw_body, headers or {})

    elif channel == 'airwallex':
        from .airwallex import verify_airwallex_webhook
        return verify_airwallex_webhook(raw_body, headers or {})

    elif channel == 'lianlian':
        from .lianlian import verify_lianlian_notify
        return verify_lianlian_notify(raw_data, headers or {})

    else:
        return False, {'error': f'Unknown channel: {channel}'}


def process_refund(order_no: str, amount_fen: int, channel: str, trade_no: str = '') -> Dict[str, Any]:
    """统一退款入口：根据 channel 路由到对应网关

    Args:
        order_no: 原订单号
        amount_fen: 退款金额（分）
        channel: 支付渠道
        trade_no: 网关交易号

    Returns:
        Dict with keys: success, refund_no, error
    """
    if channel == 'alipay':
        from .alipay import refund_alipay_order
        return refund_alipay_order(order_no, amount_fen)

    elif channel == 'wechat':
        from .wechat import refund_wechat_order
        return refund_wechat_order(order_no, amount_fen)

    elif channel == 'stripe':
        from .stripe import refund_stripe_session
        return refund_stripe_session(trade_no, amount_fen)

    elif channel == 'paypal':
        from .paypal import refund_paypal_order
        return refund_paypal_order(trade_no, amount_fen)

    elif channel == 'adyen':
        from .adyen import refund_adyen_payment
        return refund_adyen_payment(trade_no, amount_fen)

    elif channel == 'checkoutcom':
        from .checkoutcom import refund_checkout_payment
        return refund_checkout_payment(trade_no, amount_fen)

    elif channel == 'airwallex':
        from .airwallex import refund_airwallex_payment
        return refund_airwallex_payment(trade_no, amount_fen)

    elif channel == 'lianlian':
        from .lianlian import refund_lianlian_order
        return refund_lianlian_order(trade_no, amount_fen)

    else:
        return {'success': False, 'refund_no': '', 'error': f'Unknown channel: {channel}'}


def process_charge(channel: str, agreement_id: str, order_no: str, amount_fen: int,
                   subject: str = '') -> Tuple[bool, str]:
    """统一自动扣款入口（周期扣款/委托代扣）

    Args:
        channel: alipay | wechat（目前仅 CN 渠道支持代扣）
        agreement_id: 代扣签约号（支付宝 agreement_no / 微信 contract_id）
        order_no: 商户订单号
        amount_fen: 金额（分）
        subject: 扣款描述

    Returns:
        Tuple[bool, str]: (success, fail_reason)
        fail-closed：网关未配置/未签约一律拒绝，严禁 mock 放行。
    """
    if channel == 'alipay':
        from .alipay import execute_charge
        return execute_charge(agreement_id, order_no, amount_fen, subject)

    elif channel == 'wechat':
        from .wechat import execute_contract_charge
        return execute_contract_charge(agreement_id, order_no, amount_fen, subject)

    else:
        return False, f'Channel {channel} does not support auto-charge'


def _is_gateway_configured(gateway_name: str) -> bool:
    """统一配置就绪检查（C-03 修复）

    未正确配置的支付网关必须拒绝所有回调，禁止 mock 放行。
    支付宝/微信配置可能来自环境变量或 system_config 表，
    因此复用各网关自身的 config getter（含 DB 兜底），而非仅检查环境变量。

    Args:
        gateway_name: alipay | wechat | stripe | paypal | adyen | checkoutcom | airwallex | lianlian

    Returns:
        True 表示网关已配置；False 表示未配置或仅占位符，调用方必须拒绝回调。
    """
    try:
        if gateway_name == 'alipay':
            from .alipay import _get_alipay_config
            cfg = _get_alipay_config()
            return bool(cfg.get('app_id') and cfg.get('private_key') and cfg.get('public_key'))

        if gateway_name == 'wechat':
            from .wechat import _get_wechat_config
            cfg = _get_wechat_config()
            return bool(cfg.get('app_id') and cfg.get('api_key'))

        if gateway_name == 'stripe':
            from .stripe import _get_stripe_config
            cfg = _get_stripe_config()
            sk = cfg.get('secret_key') or ''
            secret = cfg.get('webhook_secret') or ''
            # L-03：改用占位符模式检测，避免把真实 sk_live_/whsec_ 前缀密钥误判为未配置
            return bool(sk and secret) \
                and not _is_placeholder(sk) \
                and not _is_placeholder(secret)

        if gateway_name == 'paypal':
            from .paypal import _get_paypal_config
            cid = _get_paypal_config().get('client_id') or ''
            return bool(cid) and not _is_placeholder(cid)

        if gateway_name == 'adyen':
            from .adyen import _get_adyen_config
            cfg = _get_adyen_config()
            return bool(cfg.get('api_key') and cfg.get('hmac_key') and cfg.get('merchant_account')) \
                and not _is_placeholder(cfg.get('api_key'))

        if gateway_name == 'checkoutcom':
            from .checkoutcom import _get_checkout_config
            cfg = _get_checkout_config()
            return bool(cfg.get('secret_key') and cfg.get('webhook_key')) \
                and not _is_placeholder(cfg.get('secret_key'))

        if gateway_name == 'airwallex':
            from .airwallex import _get_airwallex_config
            cfg = _get_airwallex_config()
            return bool(cfg.get('client_id') and cfg.get('api_key') and cfg.get('webhook_secret')) \
                and not _is_placeholder(cfg.get('api_key'))

        if gateway_name == 'lianlian':
            from .lianlian import _get_lianlian_config
            cfg = _get_lianlian_config()
            return bool(cfg.get('merchant_id') and cfg.get('sub_merchant_id')
                        and cfg.get('private_key') and cfg.get('public_key')) \
                and not _is_placeholder(cfg.get('merchant_id'))
    except Exception:
        return False
    return False
