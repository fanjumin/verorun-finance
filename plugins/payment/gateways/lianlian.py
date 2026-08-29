#!/usr/bin/env python3
"""
Payment Plugin — 连连国际（连连全球收单）支付网关
==================================================
中国区 (DEPLOY_MARKET=cn) 跨境收单渠道。
接口: 连连全球收单 v3 — API-支付 / API-退款（收银台模式，跳转支付）
纯网关：仅下单 / 验签 / 退款，不承载任何业务逻辑。
签名：SHA1withRSA，签名因子 = body 全部参数递归排序后扁平拼接（& 连接）。
注意：连连金额单位为「元」（2 位小数），模块内由 amount_fen 转换。
"""

import os
import json
import time
import base64
import urllib.request
from typing import Dict, Any, Tuple


def _get_lianlian_config() -> dict:
    """从环境变量或主库 system_config 读取连连全球收单配置"""
    cfg = {
        'merchant_id': os.environ.get('LIANLIAN_MERCHANT_ID', ''),
        'sub_merchant_id': os.environ.get('LIANLIAN_SUB_MERCHANT_ID', ''),
        'private_key': os.environ.get('LIANLIAN_PRIVATE_KEY', ''),
        'public_key': os.environ.get('LIANLIAN_PUBLIC_KEY', ''),
        'country': os.environ.get('LIANLIAN_COUNTRY', 'US'),
        'currency': os.environ.get('LIANLIAN_CURRENCY', 'USD'),
        'notify_base': os.environ.get('NOTIFY_BASE', ''),
        'environment': os.environ.get('LIANLIAN_ENVIRONMENT', 'sandbox'),
    }

    if not cfg['merchant_id']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'merchant_id': 'lianlian_merchant_id',
            'sub_merchant_id': 'lianlian_sub_merchant_id',
            'private_key': 'lianlian_private_key',
            'public_key': 'lianlian_public_key',
            'country': 'lianlian_country',
            'currency': 'lianlian_currency',
            'notify_base': 'payment.notify_base',
            'environment': 'lianlian_environment',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def _base_url(environment: str) -> str:
    """连连全球收单 API 环境基地址"""
    if environment == 'live':
        return 'https://gpapi.lianlianpay.com'
    return 'https://celer-api.LianLianpay-inc.com'


def _ensure_pem(key_str: str, key_type: str = 'PRIVATE KEY') -> str:
    """确保密钥为 PEM 格式"""
    if not key_str:
        return ''
    if '-----BEGIN' in key_str:
        return key_str
    lines = [key_str[i:i + 64] for i in range(0, len(key_str), 64)]
    return f'-----BEGIN {key_type}-----\n' + '\n'.join(lines) + f'\n-----END {key_type}-----\n'


def _flatten_sign_items(value: Any, prefix: str = None) -> list:
    """连连签名因子扁平化（递归）

    规则（官方）：
    - dict 按 key 字母正序；NULL 字段不参与签名，空字符串参与
    - list 直接展开元素，不保留数组名
    - 标量输出 'key=value'
    """
    items = []
    if isinstance(value, dict):
        for k in sorted(value.keys()):
            v = value[k]
            if v is None:
                continue  # NULL 不参与签名
            items.extend(_flatten_sign_items(v, k))
    elif isinstance(value, list):
        for item in value:
            items.extend(_flatten_sign_items(item, None))
    else:
        items.append(f'{prefix}={value}')
    return items


def _sign(payload: dict, private_key: str) -> str:
    """连连全球收单请求签名（SHA1withRSA，PKCS8 私钥）"""
    sign_str = '&'.join(_flatten_sign_items(payload))
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key_pem = _ensure_pem(private_key, 'PRIVATE KEY')
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        signature = key.sign(sign_str.encode(), padding.PKCS1v15(), hashes.SHA1())
        return base64.b64encode(signature).decode()
    except ImportError:
        raise RuntimeError(
            '[LianLian] cryptography library is required for RSA signing. '
            'Install with: pip install cryptography'
        )


def _verify(payload: dict, signature: str, public_key: str) -> bool:
    """连连通知验签（SHA1withRSA，连连公钥）"""
    sign_str = '&'.join(_flatten_sign_items(payload))
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key_pem = _ensure_pem(public_key, 'PUBLIC KEY')
        key = serialization.load_pem_public_key(key_pem.encode())
        key.verify(base64.b64decode(signature), sign_str.encode(),
                   padding.PKCS1v15(), hashes.SHA1())
        return True
    except Exception:
        return False


def _now_ts() -> str:
    """yyyyMMddHHmmss"""
    return time.strftime('%Y%m%d%H%M%S')


def _post_json(url: str, payload: dict, cfg: dict) -> dict:
    """统一 JSON POST（附连连签名 Header），失败抛异常由调用方 fail-closed 处理"""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Content-Type': 'application/json',
            'signature': _sign(payload, cfg['private_key']),
            'timezone': 'Asia/Shanghai',
            'timestamp': _now_ts(),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def create_lianlian_order(order_no: str, amount_fen: int, subject: str,
                          description: str) -> Dict[str, Any]:
    """创建连连全球收单支付订单（收银台模式，返回跳转 URL）

    Returns:
        Dict with keys: success, trade_no, qr_code, redirect_url, error
    """
    from . import _is_placeholder
    cfg = _get_lianlian_config()

    if not cfg['merchant_id'] or _is_placeholder(cfg['merchant_id']):
        # C-02：未配置不再返回 mock，避免产生无法支付的 pending 订单
        print('[LianLian] Not configured, cannot create payment')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': 'LianLian gateway not configured',
        }

    try:
        notify_base = cfg['notify_base']
        if not notify_base:
            raise RuntimeError('LianLian notify_base (NOTIFY_BASE) is required')

        amount_yuan = round(amount_fen / 100, 2)
        merchant_order = {
            'merchant_order_id': order_no,
            'merchant_order_time': _now_ts(),
            'order_description': (subject or description or '')[:256],
            'order_amount': amount_yuan,
            'order_currency_code': cfg['currency'],
            'products': [
                {
                    'product_id': f'PROD-{order_no}'[:64],
                    'name': (subject or description or 'Product')[:64],
                    'price': amount_yuan,
                    'quantity': 1,
                }
            ],
        }
        payload = {
            'merchant_transaction_id': order_no,
            'merchant_id': cfg['merchant_id'],
            'sub_merchant_id': cfg['sub_merchant_id'],
            'notification_url': f'{notify_base}/plugin/subscription/api/notify/lianlian',
            'redirect_url': os.environ.get('SUCCESS_URL',
                                           f'{notify_base}/subscribe/success'),
            'cancel_url': os.environ.get('CANCEL_URL',
                                         f'{notify_base}/subscribe/cancel'),
            'country': cfg['country'],
            'merchant_order': merchant_order,
        }

        url = f'{_base_url(cfg["environment"])}/v3/merchants/{cfg["merchant_id"]}/payments'
        resp = _post_json(url, payload, cfg)

        if resp.get('return_code') != 'SUCCESS':
            return {
                'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
                'error': resp.get('return_message', resp.get('return_code', 'unknown')),
            }

        order = resp.get('order', {}) or {}
        return {
            'success': True,
            'trade_no': order.get('ll_transaction_id', ''),
            'qr_code': order.get('qrcode', ''),
            'redirect_url': order.get('payment_url', ''),
        }
    except Exception as e:
        print(f'[LianLian] Error: {e}')
        return {
            'success': False, 'trade_no': '', 'qr_code': '', 'redirect_url': '',
            'error': str(e),
        }


def verify_lianlian_notify(raw_data, headers: dict) -> Tuple[bool, dict]:
    """验证连连支付结果通知（RSA-SHA1，连连公钥）并解析结果

    Returns:
        Tuple[bool, dict]: (is_valid, parsed_data)
    """
    from . import _is_gateway_configured
    cfg = _get_lianlian_config()

    if not _is_gateway_configured('lianlian'):
        # 未配置一律拒绝回调，禁止放行
        print('[LianLian] SECURITY: public key not configured, rejecting notify')
        return False, {}

    try:
        body = raw_data.decode('utf-8') if isinstance(raw_data, bytes) else raw_data
        if isinstance(body, str):
            params = json.loads(body) if body else {}
        else:
            params = body or {}

        # 兼容：signature 在 Header 或 body 内
        signature = headers.get('signature') or headers.get('Signature-Data') or ''
        verify_params = dict(params)
        verify_params.pop('signature', None)

        if not signature or not _verify(verify_params, signature, cfg['public_key']):
            print('[LianLian] SECURITY: notify signature mismatch, rejecting')
            return False, {}

        # payment_status: PS=支付成功 / PP=支付处理中
        order = params.get('order', {}) or {}
        pay_data = order.get('payment_data', {}) or {}
        pay_status = pay_data.get('payment_status', '')
        if pay_status == 'PS':
            return True, {
                'order_no': params.get('merchant_transaction_id')
                            or order.get('merchant_transaction_id', ''),
                'trade_no': order.get('ll_transaction_id', ''),
                'status': 'paid',
                'total_fee': int(round(float(pay_data.get('payment_amount') or 0) * 100)),
            }
        return False, {}
    except Exception as e:
        print(f'[LianLian] Notify verify error: {e}')
        return False, {}


def refund_lianlian_order(trade_no: str, amount_fen: int = 0) -> Dict[str, Any]:
    """连连退款（trade_no = 原商户支付交易 ID = order_no）

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    from . import _is_placeholder
    cfg = _get_lianlian_config()

    if not cfg['merchant_id'] or _is_placeholder(cfg['merchant_id']):
        print('[LianLian Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False, 'refund_no': '',
            'error': 'LianLian gateway not configured; refund requires manual processing',
        }

    try:
        refund_txn_id = f'REF{trade_no}'[:64]
        payload = {
            'merchant_transaction_id': refund_txn_id,
            'merchant_id': cfg['merchant_id'],
            'sub_merchant_id': cfg['sub_merchant_id'],
            'merchant_refund_time': _now_ts(),
            'original_transaction_id': trade_no,
            'refund_data': {
                'refund_amount': round(amount_fen / 100, 2),
                'refund_currency_code': cfg['currency'],
            },
        }
        url = (f'{_base_url(cfg["environment"])}/v3/merchants/{cfg["merchant_id"]}'
               f'/payments/{trade_no}/refunds')
        resp = _post_json(url, payload, cfg)

        if resp.get('return_code') != 'SUCCESS':
            return {
                'success': False, 'refund_no': '',
                'error': resp.get('return_message', resp.get('return_code', 'unknown')),
            }

        order = resp.get('order', {}) or {}
        refund_status = ((order.get('refund_data') or {}).get('refund_status') or '')
        success = refund_status in ('RS', 'RP')  # RS=成功 / RP=处理中
        return {
            'success': success,
            'refund_no': order.get('ll_transaction_id', ''),
            'error': '' if success else f'LianLian refund status: {refund_status}',
        }
    except Exception as e:
        print(f'[LianLian Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


# ═══ 兼容别名（与 stripe.py 的 refund_order 签名约定一致） ═══
refund_order = refund_lianlian_order
