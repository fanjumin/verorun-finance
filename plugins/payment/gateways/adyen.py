#!/usr/bin/env python3
"""
Payment Plugin — Adyen 支付网关
=================================
国际区 (DEPLOY_MARKET=intl) 企业级支付渠道。
接口: Adyen Checkout API v71 — Sessions flow（托管收银台）
纯网关：仅下单 / 验签 / 退款，不承载任何业务逻辑。
"""

import os
import base64
import hashlib
import hmac
import json
import urllib.request
from typing import Dict, Any, Tuple


def _get_adyen_config() -> dict:
    """从环境变量或主库 system_config 读取 Adyen 配置"""
    cfg = {
        'api_key': os.environ.get('ADYEN_API_KEY', ''),
        'hmac_key': os.environ.get('ADYEN_HMAC_KEY', ''),
        'merchant_account': os.environ.get('ADYEN_MERCHANT_ACCOUNT', ''),
        'client_key': os.environ.get('ADYEN_CLIENT_KEY', ''),
        'currency': os.environ.get('ADYEN_CURRENCY', 'USD'),
        'environment': os.environ.get('ADYEN_ENVIRONMENT', 'sandbox'),
    }

    # H-03：环境变量缺失时从 system_config 表读取兜底
    if not cfg['api_key']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'api_key': 'adyen_api_key',
            'hmac_key': 'adyen_hmac_key',
            'merchant_account': 'adyen_merchant_account',
            'client_key': 'adyen_client_key',
            'currency': 'adyen_currency',
            'environment': 'adyen_environment',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def _base_url(environment: str) -> str:
    """Adyen Checkout API 环境基地址"""
    if environment == 'live':
        return 'https://checkout-live.adyenpayments.com/checkout'
    return 'https://checkout-test.adyen.com'


def _post_json(url: str, payload: dict, api_key: str) -> dict:
    """统一 JSON POST（失败抛异常，由调用方 fail-closed 处理）"""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def create_adyen_session(order_no: str, amount_fen: int, subject: str,
                         description: str) -> Dict[str, Any]:
    """创建 Adyen Checkout Session（托管收银台）

    Returns:
        Dict with keys: success, trade_no, qr_code, redirect_url, error
    """
    from . import _is_placeholder
    cfg = _get_adyen_config()
    api_key = cfg['api_key']

    if not api_key or _is_placeholder(api_key):
        # C-02：未配置不再返回 mock，避免产生无法支付的 pending 订单
        print('[Adyen] Not configured, cannot create payment')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': 'Adyen gateway not configured',
        }

    try:
        return_url = os.environ.get('ADYEN_RETURN_URL') or os.environ.get(
            'SUCCESS_URL', '/subscribe/success')
        payload = {
            'amount': {'currency': cfg['currency'], 'value': int(amount_fen)},
            'merchantAccount': cfg['merchant_account'],
            'reference': order_no,
            'returnUrl': return_url,
            'channel': 'Web',
        }
        url = f'{_base_url(cfg["environment"])}/checkout/v71/sessions'
        resp = _post_json(url, payload, api_key)

        return {
            'success': True,
            'trade_no': resp.get('id', ''),
            'qr_code': '',
            'redirect_url': resp.get('url', ''),
        }
    except Exception as e:
        print(f'[Adyen] Error: {e}')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': str(e),
        }


def verify_adyen_webhook(raw_body, headers: dict) -> Tuple[bool, dict]:
    """验证 Adyen Webhook（HMAC-SHA256）并解析授权结果

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    from . import _is_gateway_configured
    cfg = _get_adyen_config()

    if not _is_gateway_configured('adyen'):
        # 未配置一律拒绝回调，禁止放行
        print('[Adyen] SECURITY: webhook key not configured, rejecting webhook')
        return False, {}

    try:
        body_bytes = raw_body.encode('utf-8') if isinstance(raw_body, str) else bytes(raw_body or b'')
        if not body_bytes:
            return False, {}

        sig = headers.get('HmacSignature', '')
        if not sig:
            return False, {}

        # 官方验签：base64(HMAC-SHA256(raw_body, hmac_key))
        expected = base64.b64encode(
            hmac.new(cfg['hmac_key'].encode('utf-8'), body_bytes, hashlib.sha256).digest()
        ).decode('utf-8')
        if not hmac.compare_digest(expected, sig):
            print('[Adyen] SECURITY: webhook signature mismatch, rejecting')
            return False, {}

        data = json.loads(body_bytes.decode('utf-8'))
        items = data.get('notificationItems') or []
        if not items:
            return False, {}
        item = items[0].get('NotificationRequestItem', {})

        event_code = item.get('eventCode', '')
        success = item.get('success', '')
        if event_code == 'AUTHORISATION' and success == 'true':
            amount = item.get('amount', {}) or {}
            return True, {
                'order_no': item.get('merchantReference', ''),
                'trade_no': item.get('pspReference', ''),
                'status': 'paid',
                'total_fee': amount.get('value') or 0,
            }
        return False, {}
    except Exception as e:
        print(f'[Adyen] Webhook verify error: {e}')
        return False, {}


def refund_adyen_payment(trade_no: str, amount_fen: int = 0) -> Dict[str, Any]:
    """Adyen 退款（trade_no = 支付成功的 pspReference）

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    from . import _is_placeholder
    cfg = _get_adyen_config()
    api_key = cfg['api_key']

    if not api_key or _is_placeholder(api_key):
        print('[Adyen Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False, 'refund_no': '',
            'error': 'Adyen gateway not configured; refund requires manual processing',
        }

    try:
        payload = {
            'merchantAccount': cfg['merchant_account'],
            'amount': {'currency': cfg['currency'], 'value': int(amount_fen)},
        }
        url = f'{_base_url(cfg["environment"])}/checkout/v71/payments/{trade_no}/refunds'
        resp = _post_json(url, payload, api_key)

        status = resp.get('status', '')
        success = status in ('received', 'authorised')
        return {
            'success': success,
            'refund_no': resp.get('pspReference', ''),
            'error': '' if success else f'Adyen refund status: {status}',
        }
    except Exception as e:
        print(f'[Adyen Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


# ═══ 兼容别名（与 stripe.py 的 refund_order 签名约定一致） ═══
refund_order = refund_adyen_payment
