#!/usr/bin/env python3
"""
Payment Plugin — Checkout.com 支付网关
=======================================
国际区 (DEPLOY_MARKET=intl) 支付渠道。
接口: Checkout.com Payments API — /payments（含 3DS 重定向）
纯网关：仅下单 / 验签 / 退款，不承载任何业务逻辑。
"""

import os
import hmac
import hashlib
import json
import time
import urllib.request
from typing import Dict, Any, Tuple


def _get_checkout_config() -> dict:
    """从环境变量或主库 system_config 读取 Checkout.com 配置"""
    cfg = {
        'secret_key': os.environ.get('CHECKOUT_SECRET_KEY', ''),
        'public_key': os.environ.get('CHECKOUT_PUBLIC_KEY', ''),
        'webhook_key': os.environ.get('CHECKOUT_WEBHOOK_KEY', ''),
        'processing_channel_id': os.environ.get('CHECKOUT_PROCESSING_CHANNEL_ID', ''),
        'currency': os.environ.get('CHECKOUT_CURRENCY', 'USD'),
        'environment': os.environ.get('CHECKOUT_ENVIRONMENT', 'sandbox'),
    }

    if not cfg['secret_key']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'secret_key': 'checkoutcom_secret_key',
            'public_key': 'checkoutcom_public_key',
            'webhook_key': 'checkoutcom_webhook_key',
            'processing_channel_id': 'checkoutcom_processing_channel_id',
            'currency': 'checkoutcom_currency',
            'environment': 'checkoutcom_environment',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def _base_url(environment: str) -> str:
    return 'https://api.sandbox.checkout.com' if environment != 'live' else 'https://api.checkout.com'


def _post_json(url: str, payload: dict, secret_key: str) -> dict:
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': secret_key},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def create_checkout_session(order_no: str, amount_fen: int, subject: str,
                            description: str) -> Dict[str, Any]:
    """创建 Checkout.com 支付（/payments，需要 3DS 时返回跳转链接）

    Returns:
        Dict with keys: success, trade_no, qr_code, redirect_url, error
    """
    from . import _is_placeholder
    cfg = _get_checkout_config()
    sk = cfg['secret_key']

    if not sk or _is_placeholder(sk):
        # C-02：未配置不再返回 mock，避免产生无法支付的 pending 订单
        print('[Checkout.com] Not configured, cannot create payment')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': 'Checkout.com gateway not configured',
        }

    try:
        payload = {
            'amount': {'value': int(amount_fen), 'currency': cfg['currency']},
            'reference': order_no,
            'payment_type': 'Regular',
            'success_url': os.environ.get('SUCCESS_URL', '/subscribe/success'),
            'failure_url': os.environ.get('CANCEL_URL', '/subscribe/cancel'),
        }
        if cfg.get('processing_channel_id'):
            payload['processing_channel_id'] = cfg['processing_channel_id']

        url = f'{_base_url(cfg["environment"])}/payments'
        resp = _post_json(url, payload, sk)

        links = resp.get('_links', {}) or {}
        redirect = (links.get('redirect') or {}).get('href') or ''
        return {
            'success': True,
            'trade_no': resp.get('id', ''),
            'qr_code': '',
            'redirect_url': redirect,
        }
    except Exception as e:
        print(f'[Checkout.com] Error: {e}')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': str(e),
        }


def verify_checkout_webhook(raw_body, headers: dict) -> Tuple[bool, dict]:
    """验证 Checkout.com Webhook 签名并解析支付结果

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    from . import _is_gateway_configured
    cfg = _get_checkout_config()

    if not _is_gateway_configured('checkoutcom'):
        # 未配置一律拒绝回调，禁止放行
        print('[Checkout.com] SECURITY: webhook key not configured, rejecting webhook')
        return False, {}

    try:
        body = raw_body.decode('utf-8') if isinstance(raw_body, bytes) else str(raw_body or '')
        if not body:
            return False, {}

        sig_header = headers.get('Cko-Signature', '')
        if not sig_header:
            return False, {}

        # 官方签名格式: {algorithm};{keyid};{signature};{timestamp}
        parts = sig_header.split(';')
        if len(parts) != 4 or parts[0].upper() != 'HMACSHA256':
            print('[Checkout.com] SECURITY: unsupported signature algorithm')
            return False, {}
        signature, timestamp = parts[2], parts[3]

        # 防重放：时间戳偏差 > 5 分钟拒绝
        try:
            if abs(time.time() - int(timestamp)) > 300:
                print('[Checkout.com] SECURITY: webhook timestamp out of range')
                return False, {}
        except ValueError:
            return False, {}

        # 官方验签：HMAC-SHA256 over "{timestamp}.{body}"，hex 对比
        signed_payload = f'{timestamp}.{body}'
        expected = hmac.new(
            cfg['webhook_key'].encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            print('[Checkout.com] SECURITY: webhook signature mismatch, rejecting')
            return False, {}

        event = json.loads(body)
        event_type = event.get('type', '')
        if event_type == 'payment_approved':
            data = event.get('data', {}) or {}
            amount = data.get('amount', {}) or {}
            return True, {
                'order_no': data.get('reference', ''),
                'trade_no': data.get('id', ''),
                'status': 'paid',
                'total_fee': amount.get('value') or 0,
            }
        return False, {}
    except Exception as e:
        print(f'[Checkout.com] Webhook verify error: {e}')
        return False, {}


def refund_checkout_payment(trade_no: str, amount_fen: int = 0) -> Dict[str, Any]:
    """Checkout.com 退款（trade_no = 支付成功的 payment id）

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    from . import _is_placeholder
    cfg = _get_checkout_config()
    sk = cfg['secret_key']

    if not sk or _is_placeholder(sk):
        print('[Checkout.com Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False, 'refund_no': '',
            'error': 'Checkout.com gateway not configured; refund requires manual processing',
        }

    try:
        payload = {
            'amount': {'value': int(amount_fen), 'currency': cfg['currency']},
            'reference': trade_no,
        }
        url = f'{_base_url(cfg["environment"])}/payments/{trade_no}/refunds'
        resp = _post_json(url, payload, sk)

        approved = bool(resp.get('approved', False))
        return {
            'success': approved,
            'refund_no': resp.get('id', ''),
            'error': '' if approved else f'Checkout.com refund status: {resp.get("status", "")}',
        }
    except Exception as e:
        print(f'[Checkout.com Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


# ═══ 兼容别名（与 stripe.py 的 refund_order 签名约定一致） ═══
refund_order = refund_checkout_payment
