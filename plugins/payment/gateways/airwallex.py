#!/usr/bin/env python3
"""
Payment Plugin — Airwallex 空中云汇 支付网关
=============================================
中国区 (DEPLOY_MARKET=cn) 跨境收单渠道。
接口: Airwallex PaymentIntents API + Hosted Payment Page（前端 Airwallex.js）
纯网关：仅创建 PaymentIntent / 验签 / 退款，不承载任何业务逻辑。
注意：Airwallex 金额单位为「元」，模块内由 amount_fen 转换。
"""

import os
import hmac
import hashlib
import json
import urllib.request
from typing import Dict, Any, Tuple


def _get_airwallex_config() -> dict:
    """从环境变量或主库 system_config 读取 Airwallex 配置"""
    cfg = {
        'client_id': os.environ.get('AIRWALLEX_CLIENT_ID', ''),
        'api_key': os.environ.get('AIRWALLEX_API_KEY', ''),
        'webhook_secret': os.environ.get('AIRWALLEX_WEBHOOK_SECRET', ''),
        'currency': os.environ.get('AIRWALLEX_CURRENCY', 'USD'),
        'environment': os.environ.get('AIRWALLEX_ENVIRONMENT', 'sandbox'),
    }

    if not cfg['client_id']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'client_id': 'airwallex_client_id',
            'api_key': 'airwallex_api_key',
            'webhook_secret': 'airwallex_webhook_secret',
            'currency': 'airwallex_currency',
            'environment': 'airwallex_environment',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def _base_url(environment: str) -> str:
    return 'https://api-demo.airwallex.com' if environment != 'live' else 'https://api.airwallex.com'


def _get_access_token(cfg: dict) -> str:
    """OAuth 登录换取 Bearer token（每次下单重新获取，避免过期）"""
    url = f'{_base_url(cfg["environment"])}/api/v1/authentication/login'
    req = urllib.request.Request(
        url, data=b'', method='POST',
        headers={
            'Content-Type': 'application/json',
            'x-client-id': cfg['client_id'],
            'x-api-key': cfg['api_key'],
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    token = data.get('token', '')
    if not token:
        raise RuntimeError('Airwallex login failed: no token')
    return token


def create_airwallex_intent(order_no: str, amount_fen: int, subject: str,
                            description: str) -> Dict[str, Any]:
    """创建 Airwallex PaymentIntent

    Returns:
        Dict: success, trade_no(PaymentIntent id), qr_code, redirect_url,
              client_secret, currency（供前端 Airwallex.js 拉起托管收银台）
    """
    from . import _is_placeholder
    cfg = _get_airwallex_config()

    if not cfg['client_id'] or _is_placeholder(cfg['client_id']):
        # C-02：未配置不再返回 mock，避免产生无法支付的 pending 订单
        print('[Airwallex] Not configured, cannot create payment')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': 'Airwallex gateway not configured',
        }

    try:
        token = _get_access_token(cfg)
        url = f'{_base_url(cfg["environment"])}/api/v1/pa/payment_intents/create'
        payload = {
            'request_id': order_no,
            'merchant_order_id': order_no,
            'amount': round(amount_fen / 100, 2),
            'currency': cfg['currency'],
            'return_url': os.environ.get('SUCCESS_URL', '/subscribe/success'),
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        return {
            'success': True,
            'trade_no': result.get('id', ''),
            'qr_code': '',
            'redirect_url': '',
            'client_secret': result.get('client_secret', ''),
            'currency': result.get('currency', cfg['currency']),
        }
    except Exception as e:
        print(f'[Airwallex] Error: {e}')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': str(e),
        }


def verify_airwallex_webhook(raw_body, headers: dict) -> Tuple[bool, dict]:
    """验证 Airwallex Webhook 签名并解析支付成功事件

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    from . import _is_gateway_configured
    cfg = _get_airwallex_config()

    if not _is_gateway_configured('airwallex'):
        # 未配置一律拒绝回调，禁止放行
        print('[Airwallex] SECURITY: webhook secret not configured, rejecting webhook')
        return False, {}

    try:
        body = raw_body.decode('utf-8') if isinstance(raw_body, bytes) else str(raw_body or '')
        if not body:
            return False, {}

        signature = headers.get('x-signature', '')
        if not signature:
            return False, {}

        # 官方验签：HMAC-SHA256(raw_body, webhook_secret)，hex 对比
        expected = hmac.new(
            cfg['webhook_secret'].encode('utf-8'),
            body.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            print('[Airwallex] SECURITY: webhook signature mismatch, rejecting')
            return False, {}

        event = json.loads(body)
        name = event.get('name', '')
        if name == 'payment_intent.succeeded':
            obj = (event.get('data', {}) or {}).get('object', {}) or {}
            status = obj.get('status', '')
            if status == 'SUCCEEDED':
                # 金额单位：元 → 分
                total_fee = int(round(float(obj.get('amount') or 0) * 100))
                return True, {
                    'order_no': obj.get('merchant_order_id', ''),
                    'trade_no': obj.get('id', ''),
                    'status': 'paid',
                    'total_fee': total_fee,
                }
        return False, {}
    except Exception as e:
        print(f'[Airwallex] Webhook verify error: {e}')
        return False, {}


def refund_airwallex_payment(trade_no: str, amount_fen: int = 0) -> Dict[str, Any]:
    """Airwallex 退款（trade_no = PaymentIntent id）

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    from . import _is_placeholder
    cfg = _get_airwallex_config()

    if not cfg['client_id'] or _is_placeholder(cfg['client_id']):
        print('[Airwallex Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False, 'refund_no': '',
            'error': 'Airwallex gateway not configured; refund requires manual processing',
        }

    try:
        token = _get_access_token(cfg)
        url = f'{_base_url(cfg["environment"])}/api/v1/pa/refunds/create'
        payload = {
            'request_id': f'refund-{trade_no}',
            'payment_intent_id': trade_no,
            'amount': round(amount_fen / 100, 2),
            'currency': cfg['currency'],
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        status = result.get('status', '')
        success = status in ('PROCESSING', 'SUCCEEDED')
        return {
            'success': success,
            'refund_no': result.get('id', ''),
            'error': '' if success else f'Airwallex refund status: {status}',
        }
    except Exception as e:
        print(f'[Airwallex Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


# ═══ 兼容别名（与 stripe.py 的 refund_order 签名约定一致） ═══
refund_order = refund_airwallex_payment
