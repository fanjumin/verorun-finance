#!/usr/bin/env python3
"""
Payment Gateway — PayPal 支付网关
=======================================
国际区 (DEPLOY_MARKET=intl) 备用支付渠道。
接口: PayPal REST API v2 Orders
"""

import os
import json
import time
import base64
from typing import Dict, Any, Tuple


def _get_paypal_config() -> dict:
    cfg = {
        'client_id': os.environ.get('PAYPAL_CLIENT_ID', ''),
        'client_secret': os.environ.get('PAYPAL_CLIENT_SECRET', ''),
        'mode': os.environ.get('PAYPAL_MODE', 'sandbox'),  # sandbox | live
        'webhook_id': os.environ.get('PAYPAL_WEBHOOK_ID', ''),   # C1: PayPal Webhook ID（Dashboard → Apps → Webhooks）
    }

    # H-03：环境变量缺失时从 system_config 表读取兜底
    if not cfg['client_id']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'client_id': 'paypal_client_id',
            'client_secret': 'paypal_client_secret',
            'mode': 'paypal_mode',
            'webhook_id': 'paypal_webhook_id',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def _get_api_base() -> str:
    cfg = _get_paypal_config()
    if cfg['mode'] == 'live':
        return 'https://api-m.paypal.com'
    return 'https://api-m.sandbox.paypal.com'


def _get_access_token() -> str:
    """获取 PayPal OAuth 2.0 Access Token"""
    cfg = _get_paypal_config()
    api_base = _get_api_base()

    auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()

    try:
        import urllib.request
        data = urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode()
        req = urllib.request.Request(f'{api_base}/v1/oauth2/token', data=data, method='POST')
        req.add_header('Authorization', f'Basic {auth}')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        return body.get('access_token', '')
    except Exception as e:
        print(f'[PayPal] Token error: {e}')
        return ''


def create_paypal_order(order_no: str, amount_fen: int, subject: str,
                        description: str, interval_type: str = 'month') -> Dict[str, Any]:
    """创建 PayPal Order

    Returns:
        Dict with redirect_url for client approval.
    """
    from . import _is_placeholder
    cfg = _get_paypal_config()
    if not cfg['client_id'] or _is_placeholder(cfg['client_id']):
        # C-02：未配置不再返回 mock 跳转，避免产生无法支付的 pending 订单
        print('[PayPal] Not configured, cannot create payment')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': 'PayPal gateway not configured',
        }

    token = _get_access_token()
    if not token:
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': 'Failed to get access token',
        }

    api_base = _get_api_base()
    amount_usd = f'{amount_fen / 100:.2f}'
    return_url = os.environ.get('SUCCESS_URL', '/subscribe/success')
    cancel_url = os.environ.get('CANCEL_URL', '/subscribe/cancel')

    order_data = {
        'intent': 'CAPTURE',
        'purchase_units': [{
            'reference_id': order_no,
            'description': description,
            'amount': {
                'currency_code': 'USD',
                'value': amount_usd,
            },
        }],
        'application_context': {
            'brand_name': 'VeroRun',
            'landing_page': 'NO_PREFERENCE',
            'user_action': 'PAY_NOW',
            'return_url': return_url,
            'cancel_url': cancel_url,
        },
    }

    try:
        import urllib.request
        data = json.dumps(order_data).encode()
        req = urllib.request.Request(f'{api_base}/v2/checkout/orders', data=data, method='POST')
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read().decode())

        # 获取 approval URL
        approval_url = ''
        for link in body.get('links', []):
            if link.get('rel') == 'approve':
                approval_url = link.get('href', '')
                break

        return {
            'success': True,
            'trade_no': body.get('id', ''),
            'qr_code': '',
            'redirect_url': approval_url,
        }

    except Exception as e:
        print(f'[PayPal] Order creation error: {e}')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': str(e),
        }


def refund_paypal_order(trade_no: str, amount_fen: int = 0) -> Dict[str, Any]:
    """PayPal 退款

    Args:
        trade_no: PayPal Order ID
        amount_fen: 退款金额（分），0 表示全额退款

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    from . import _is_placeholder
    cfg = _get_paypal_config()
    if not cfg['client_id'] or _is_placeholder(cfg['client_id']):
        # C-01：未配置不再返回 mock 退款成功，否则订单被标记 refunded 但资金未退回
        print('[PayPal Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False,
            'refund_no': '',
            'error': 'PayPal gateway not configured; refund requires manual processing',
        }

    token = _get_access_token()
    if not token:
        return {'success': False, 'refund_no': '', 'error': 'Failed to get access token'}

    api_base = _get_api_base()
    amount_usd = f'{amount_fen / 100:.2f}'

    # 先捕获订单的 capture ID
    try:
        import urllib.request
        # 获取订单详情
        req = urllib.request.Request(f'{api_base}/v2/checkout/orders/{trade_no}', method='GET')
        req.add_header('Authorization', f'Bearer {token}')
        resp = urllib.request.urlopen(req, timeout=10)
        order_data = json.loads(resp.read().decode())

        # 找到 capture ID
        capture_id = ''
        for pu in order_data.get('purchase_units', []):
            for cap in pu.get('payments', {}).get('captures', []):
                capture_id = cap.get('id', '')
                break
            if capture_id:
                break

        if not capture_id:
            return {'success': False, 'refund_no': '', 'error': 'No capture found for this order'}

        # 执行退款
        refund_data = {}
        if amount_fen > 0:
            refund_data = {
                'amount': {
                    'value': amount_usd,
                    'currency_code': 'USD',
                }
            }

        data = json.dumps(refund_data).encode() if refund_data else b''
        req = urllib.request.Request(
            f'{api_base}/v2/payments/captures/{capture_id}/refund',
            data=data,
            method='POST',
        )
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        resp = urllib.request.urlopen(req, timeout=15)
        refund_result = json.loads(resp.read().decode())

        if refund_result.get('status') == 'COMPLETED':
            return {'success': True, 'refund_no': refund_result.get('id', ''), 'error': ''}

        return {'success': False, 'refund_no': '', 'error': refund_result.get('status', 'refund failed')}

    except Exception as e:
        print(f'[PayPal Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


def verify_paypal_webhook(raw_body: bytes, raw_data: dict, headers: dict) -> Tuple[bool, dict]:
    """验证 PayPal Webhook — 调用 PayPal 官方 verify-webhook-signature 接口

    必须环境变量：
        PAYPAL_WEBHOOK_ID  — 在 PayPal Developer Dashboard 中注册的 Webhook ID

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    webhook_id = os.environ.get('PAYPAL_WEBHOOK_ID', '').strip()
    from . import _is_placeholder
    if not webhook_id or _is_placeholder(webhook_id):
        print('[PayPal] SECURITY: PAYPAL_WEBHOOK_ID not configured, rejecting webhook')
        return False, {'error': 'PayPal webhook ID not configured'}

    # 提取 PayPal 验签专用请求头
    auth_algo       = headers.get('Paypal-Auth-Algo', '')
    cert_url        = headers.get('Paypal-Cert-Url', '')
    transmission_id = headers.get('Paypal-Transmission-Id', '')
    transmission_sig = headers.get('Paypal-Transmission-Sig', '')
    transmission_time = headers.get('Paypal-Transmission-Time', '')

    if not all([auth_algo, cert_url, transmission_id, transmission_sig, transmission_time]):
        print('[PayPal] SECURITY: missing required PayPal transmission headers')
        return False, {'error': 'Missing transmission headers'}

    # 构造 verify-webhook-signature 请求体
    verification_body = {
        'auth_algo':       auth_algo,
        'cert_url':        cert_url,
        'transmission_id': transmission_id,
        'transmission_sig': transmission_sig,
        'transmission_time': transmission_time,
        'webhook_id':      webhook_id,
        'webhook_event':   json.loads(raw_body.decode('utf-8')),
    }

    api_base = _get_api_base()
    token = _get_access_token()
    if not token:
        return False, {'error': 'Auth token unavailable'}

    try:
        import urllib.request
        data = json.dumps(verification_body).encode()
        req = urllib.request.Request(
            f'{api_base}/v1/notifications/verify-webhook-signature',
            data=data, method='POST',
        )
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())

        if result.get('verification_status') != 'SUCCESS':
            print(f'[PayPal] SECURITY: webhook verification rejected — {result}')
            return False, {'error': f'Verification status: {result.get("verification_status")}'}

    except Exception as e:
        print(f'[PayPal] SECURITY: webhook verification API call failed: {e}')
        return False, {'error': str(e)}

    # 验签通过后解析事件
    event = verification_body['webhook_event']
    event_type = event.get('event_type', '')

    if event_type not in ('PAYMENT.CAPTURE.COMPLETED', 'CHECKOUT.ORDER.APPROVED'):
        return False, {'error': f'Unhandled event type: {event_type}'}

    resource = event.get('resource', {})
    order_no = ''
    for pu in resource.get('purchase_units', []):
        order_no = pu.get('reference_id', '')
        break

    if not order_no:
        return False, {'error': 'No order_no in event'}

    return True, {
        'order_no': order_no,
        'trade_no': resource.get('id', ''),
        'status': 'paid',
    }


# ═══════════════════════════════════════════════════════════════════════════
# 兼容别名（V20260812 合流）
# 商城 shop/admin.py 依赖 A 的签名 refund_order(trade_no, amount_fen=0)
# Phase 2 重接线 shop 后仅需改 import 路径，函数签名保持不变。
# ═══════════════════════════════════════════════════════════════════════════
refund_order = refund_paypal_order
