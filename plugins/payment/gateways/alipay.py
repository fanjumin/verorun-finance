#!/usr/bin/env python3
"""
Payment Gateway — 支付宝支付网关
=======================================
中国区 (DEPLOY_MARKET=cn) 默认支付渠道。
接口: alipay.trade.precreate (扫码支付)
"""

import os
import json
import time
from datetime import datetime
from typing import Dict, Any, Tuple
from plugins._base.db import get_raw_connection


def _get_alipay_config() -> dict:
    """从 system_config 或环境变量获取支付宝配置"""
    cfg = {
        'app_id': os.environ.get('ALIPAY_APP_ID', ''),
        'private_key': os.environ.get('ALIPAY_PRIVATE_KEY', ''),
        'public_key': os.environ.get('ALIPAY_PUBLIC_KEY', ''),
        'notify_base': os.environ.get('NOTIFY_BASE', ''),
    }

    # 尝试从主库 system_config 表读取
    if not cfg['app_id']:
        try:
            import psycopg2
            import psycopg2.extras
            conn = get_raw_connection()
            conn.autocommit = False
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT key, value FROM system_config WHERE key IN "
                "('alipay_app_id', 'alipay_private_key', 'alipay_public_key', 'payment.notify_base')"
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            for r in rows:
                k = r['key']
                if k == 'alipay_app_id':
                    cfg['app_id'] = r['value']
                elif k == 'alipay_private_key':
                    cfg['private_key'] = r['value']
                elif k == 'alipay_public_key':
                    cfg['public_key'] = r['value']
                elif k == 'payment.notify_base':
                    cfg['notify_base'] = r['value']
        except Exception:
            pass

    return cfg


def _ensure_pem(key_str: str, key_type: str = 'PRIVATE KEY') -> str:
    """确保密钥为 PEM 格式"""
    if not key_str:
        return ''
    if '-----BEGIN' in key_str:
        return key_str
    lines = [key_str[i:i+64] for i in range(0, len(key_str), 64)]
    return f'-----BEGIN {key_type}-----\n' + '\n'.join(lines) + f'\n-----END {key_type}-----\n'


def _sign(params: dict, private_key: str) -> str:
    """支付宝 RSA2 签名"""
    sorted_keys = sorted(k for k in params if params[k] != '' and k != 'sign' and k != 'sign_type')
    sign_str = '&'.join(f'{k}={params[k]}' for k in sorted_keys)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
        import base64
        key_pem = _ensure_pem(private_key)
        key = serialization.load_pem_private_key(
            key_pem.encode(), password=None, backend=default_backend())
        signature = key.sign(sign_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()
    except ImportError:
        # ❌ 旧代码：降级为 sha256 摘要（无 RSA 私钥时也能"签名"成功，支付网关必然验签失败）
        raise RuntimeError(
            '[Alipay] cryptography library is required for RSA2 signing. '
            'Install with: pip install cryptography'
        )


def _verify(params: dict, signature: str, public_key: str) -> bool:
    """验证支付宝回调签名"""
    sorted_keys = sorted(k for k in params if k != 'sign' and k != 'sign_type')
    sign_str = '&'.join(f'{k}={params[k]}' for k in sorted_keys)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
        import base64
        key_pem = _ensure_pem(public_key, 'PUBLIC KEY')
        key = serialization.load_pem_public_key(key_pem.encode(), backend=default_backend())
        key.verify(base64.b64decode(signature), sign_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def create_alipay_order(order_no: str, amount_fen: int, subject: str,
                        description: str, notify_url: str = None) -> Dict[str, Any]:
    """创建支付宝扫码支付订单

    Args:
        notify_url: 自定义异步通知 URL（默认订阅回调；商城传 shop 自身回调）
    """
    cfg = _get_alipay_config()
    app_id = cfg['app_id']

    if not app_id:
        # C-02：未配置不再返回 mock 二维码，避免用户扫码后无法支付、订单永久 pending
        print('[Alipay] Not configured, cannot create payment')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': 'Alipay gateway not configured',
        }

    gateway = 'https://openapi.alipay.com/gateway.do'
    if os.environ.get('ALIPAY_SANDBOX') == 'true':
        gateway = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'

    notify_base = cfg['notify_base']
    notify_url = notify_url or (f'{notify_base}/plugin/subscription/api/notify/alipay' if notify_base else '')

    amount_yuan = f'{amount_fen / 100:.2f}'
    params = {
        'app_id': app_id,
        'method': 'alipay.trade.precreate',
        'format': 'JSON',
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'version': '1.0',
        'notify_url': notify_url,
        'biz_content': json.dumps({
            'out_trade_no': order_no,
            'total_amount': amount_yuan,
            'subject': subject,
            'body': description or subject,
            'timeout_express': '30m',
        }, ensure_ascii=False),
    }
    params['sign'] = _sign(params, cfg['private_key'])

    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(gateway, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()
        result = json.loads(body)
        response = result.get('alipay_trade_precreate_response', {})

        if response.get('code') == '10000' and response.get('qr_code'):
            return {
                'success': True,
                'trade_no': response.get('trade_no', ''),
                'qr_code': response['qr_code'],
                'redirect_url': '',
            }

        print(f'[Alipay] Order creation failed: {response.get("sub_msg", response.get("msg"))}')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': response.get('sub_msg', response.get('msg', 'unknown')),
        }
    except Exception as e:
        print(f'[Alipay] Request error: {e}')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': str(e),
        }


def refund_alipay_order(order_no: str, amount_fen: int, refund_no: str = None) -> Dict[str, Any]:
    """支付宝退款 — alipay.trade.refund

    Args:
        order_no: 原订单 out_trade_no
        amount_fen: 退款金额（分），0 表示全额退款
        refund_no: 退款请求号，不传自动生成

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    import uuid
    cfg = _get_alipay_config()
    app_id = cfg['app_id']

    if not app_id:
        # C-01：未配置不再返回 mock 退款成功，否则订单被标记 refunded 但资金未退回
        print('[Alipay Refund] NOT CONFIGURED — refund rejected')
        return {
            'success': False,
            'refund_no': '',
            'error': 'Alipay gateway not configured; refund requires manual processing',
        }

    gateway = 'https://openapi.alipay.com/gateway.do'
    if os.environ.get('ALIPAY_SANDBOX') == 'true':
        gateway = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'

    amount_yuan = f'{amount_fen / 100:.2f}'
    refund_no = refund_no or f'REF{int(time.time())}{uuid.uuid4().hex[:8].upper()}'

    params = {
        'app_id': app_id,
        'method': 'alipay.trade.refund',
        'format': 'JSON',
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'version': '1.0',
        'biz_content': json.dumps({
            'out_trade_no': order_no,
            'refund_amount': amount_yuan,
            'out_request_no': refund_no,
        }, ensure_ascii=False),
    }
    params['sign'] = _sign(params, cfg['private_key'])

    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(gateway, data=data, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()
        result = json.loads(body)
        response = result.get('alipay_trade_refund_response', {})

        if response.get('code') == '10000':
            return {'success': True, 'refund_no': refund_no, 'error': ''}

        err_msg = response.get('sub_msg', response.get('msg', 'unknown'))
        print(f'[Alipay Refund] Failed: {err_msg}')
        return {'success': False, 'refund_no': '', 'error': err_msg}
    except Exception as e:
        print(f'[Alipay Refund] Error: {e}')
        return {'success': False, 'refund_no': '', 'error': str(e)}


def verify_alipay_notify(raw_data: dict, headers: dict) -> Tuple[bool, dict]:
    """验证支付宝回调通知

    Returns:
        Tuple[bool, dict]: (is_valid, {order_no, trade_no, trade_status, total_amount})
    """
    cfg = _get_alipay_config()
    from . import _is_gateway_configured
    if not _is_gateway_configured('alipay'):
        # ❌ 旧代码：未配置时直接返回 True（认证绕过风险），此处拒绝所有回调
        print('[Alipay] SECURITY: payment gateway not configured, rejecting callback')
        return False, {}

    sign = raw_data.get('sign', '')
    if not sign:
        return False, {}

    is_valid = _verify(raw_data, sign, cfg['public_key'])
    if not is_valid:
        print('[Alipay] Signature verification failed')
        return False, {}

    trade_status = raw_data.get('trade_status', '')
    if trade_status not in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
        print(f'[Alipay] Trade status not success: {trade_status}')
        return False, {}

    # M1: 校验 app_id 与 seller_id（签名泄露后的纵深防御）
    if raw_data.get('app_id', '') != cfg['app_id']:
        print('[Alipay] SECURITY: app_id mismatch in notify')
        return False, {}

    expected_seller = os.environ.get('ALIPAY_SELLER_ID', '')
    if expected_seller and raw_data.get('seller_id', '') != expected_seller:
        print('[Alipay] SECURITY: seller_id mismatch in notify')
        return False, {}

    return True, {
        'order_no': raw_data.get('out_trade_no', ''),
        'trade_no': raw_data.get('trade_no', ''),
        'trade_status': trade_status,
        'total_amount': raw_data.get('total_amount', '0'),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 兼容别名（V20260812 合流）
# 商城 shop/admin.py 依赖 payment 网关签名 refund_order(order_no, amount_fen, refund_no)
# Phase 2 重接线 shop 后仅需改 import 路径，函数签名保持不变。
# ═══════════════════════════════════════════════════════════════════════════
refund_order = refund_alipay_order


# ═══════════════════════════════════════════════════════════════════════════
# 周期扣款（U3 续费引擎依赖 — V20260812 合流）
# 从 auth-center/routes/subscription/gateway/alipay.py 合流 execute_charge，
# 修复其 _is_stub() 假扣款成功漏洞（未配置网关时严禁 mock 放行）。
# ═══════════════════════════════════════════════════════════════════════════

def execute_charge(agreement_id: str, order_no: str, amount_fen: int,
                   subject: str = None) -> Tuple[bool, str]:
    """执行支付宝周期扣款（协议扣款 alipay.trade.pay）

    Returns:
        Tuple[bool, str]: (success, fail_reason)
    """
    from . import _is_gateway_configured
    if not _is_gateway_configured('alipay'):
        # fail-closed：A 的旧代码未配置时返回 (True, None) 造成假续费，此处拒绝
        print('[Alipay Charge] NOT CONFIGURED — charge rejected')
        return False, 'Alipay gateway not configured; charge requires manual processing'

    if not agreement_id:
        return False, 'Missing agreement id'

    cfg = _get_alipay_config()
    private_key = cfg['private_key']
    if not private_key:
        return False, 'Missing Alipay private key'

    gateway = 'https://openapi.alipay.com/gateway.do'
    if os.environ.get('ALIPAY_SANDBOX') == 'true':
        gateway = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'

    params = {
        'app_id': cfg['app_id'],
        'method': 'alipay.trade.pay',
        'format': 'JSON',
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'version': '1.0',
        'biz_content': json.dumps({
            'out_trade_no': order_no,
            'total_amount': f'{amount_fen / 100:.2f}',
            'subject': subject,
            'auth_no': agreement_id,
            'scene': 'INDUSTRY|ENERGY',
        }, ensure_ascii=False),
    }
    params['sign'] = _sign(params, private_key)

    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(gateway, data=data)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        response = result.get('alipay_trade_pay_response', {})
        if response.get('code') == '10000':
            return True, None
        elif response.get('code') in ('10003', '20000'):
            # 处理中/未知 → 主动查询
            return _poll_charge_result(order_no)
        else:
            return False, response.get('sub_msg', 'Payment failed')
    except Exception as e:
        return False, str(e)


def _poll_charge_result(order_no: str, max_retries: int = 5) -> Tuple[bool, str]:
    """主动查询支付宝支付结果"""
    cfg = _get_alipay_config()
    private_key = cfg['private_key']
    if not private_key:
        return False, 'Missing Alipay private key'

    gateway = 'https://openapi.alipay.com/gateway.do'
    if os.environ.get('ALIPAY_SANDBOX') == 'true':
        gateway = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'

    for i in range(max_retries):
        time.sleep(3)
        params = {
            'app_id': cfg['app_id'],
            'method': 'alipay.trade.query',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'biz_content': json.dumps({'out_trade_no': order_no}, ensure_ascii=False),
        }
        params['sign'] = _sign(params, private_key)

        try:
            import urllib.request
            import urllib.parse
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(gateway, data=data)
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            query_resp = result.get('alipay_trade_query_response', {})
            if query_resp.get('trade_status') == 'TRADE_SUCCESS':
                return True, None
            elif query_resp.get('trade_status') == 'TRADE_CLOSED':
                return False, 'Trading is closed'
        except Exception:
            continue
    return False, 'Query Timeout'
