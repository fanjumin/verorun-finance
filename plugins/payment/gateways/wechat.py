#!/usr/bin/env python3
"""
Payment Gateway — 微信支付网关
=====================================
中国区 (DEPLOY_MARKET=cn) 备用支付渠道。
接口: JSAPI / Native 扫码支付
"""

import os
import json
import time
import hashlib
import secrets
from typing import Dict, Any, Tuple


def _get_wechat_config() -> dict:
    cfg = {
        'app_id': os.environ.get('WECHAT_APP_ID', ''),
        'mch_id': os.environ.get('WECHAT_MCH_ID', ''),
        'api_key': os.environ.get('WECHAT_API_KEY', ''),
        'notify_base': os.environ.get('NOTIFY_BASE', ''),
    }

    # H-03：环境变量缺失时从 system_config 表读取兜底
    if not cfg['app_id']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'app_id': 'wechat_app_id',
            'mch_id': 'wechat_mch_id',
            'api_key': 'wechat_api_key',
            'notify_base': 'payment.notify_base',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value

    return cfg


def _sign_wechat(params: dict, api_key: str) -> str:
    """微信支付 MD5 签名"""
    sorted_keys = sorted(k for k in params if params[k] != '' and k != 'sign')
    sign_str = '&'.join(f'{k}={params[k]}' for k in sorted_keys)
    sign_str += f'&key={api_key}'
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()


def create_wechat_order(order_no: str, amount_fen: int, subject: str,
                        description: str, interval_type: str = 'month') -> Dict[str, Any]:
    """创建微信 Native 扫码支付订单"""
    cfg = _get_wechat_config()
    app_id = cfg['app_id']
    mch_id = cfg['mch_id']
    api_key = cfg['api_key']

    if not app_id or not mch_id or not api_key:
        # C-02：未配置不再返回 mock 二维码，避免用户扫码后无法支付、订单永久 pending
        print('[WeChat] Not configured, cannot create payment')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': 'WeChat Pay gateway not configured',
        }

    notify_base = cfg['notify_base']
    notify_url = f'{notify_base}/plugin/subscription/api/notify/wechat' if notify_base else ''

    nonce_str = secrets.token_hex(16)
    params = {
        'appid': app_id,
        'mch_id': mch_id,
        'nonce_str': nonce_str,
        'body': subject,
        'out_trade_no': order_no,
        'total_fee': amount_fen,
        'spbill_create_ip': '127.0.0.1',
        'notify_url': notify_url,
        'trade_type': 'NATIVE',
        'product_id': order_no,
    }
    params['sign'] = _sign_wechat(params, api_key)

    # 构建 XML 请求
    xml_body = '<xml>\n' + '\n'.join(f'<{k}><![CDATA[{v}]]></{k}>' for k, v in params.items()) + '\n</xml>'

    try:
        import urllib.request
        import xml.etree.ElementTree as ET

        req = urllib.request.Request(
            'https://api.mch.weixin.qq.com/pay/unifiedorder',
            data=xml_body.encode('utf-8'),
            method='POST',
        )
        req.add_header('Content-Type', 'application/xml')
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()

        root = ET.fromstring(body)

        return_code = root.find('return_code')
        if return_code is not None and return_code.text == 'SUCCESS':
            result_code = root.find('result_code')
            if result_code is not None and result_code.text == 'SUCCESS':
                code_url = root.find('code_url')
                return {
                    'success': True,
                    'trade_no': root.find('prepay_id').text if root.find('prepay_id') is not None else '',
                    'qr_code': code_url.text if code_url is not None else '',
                    'redirect_url': '',
                }

        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': root.find('return_msg').text if root.find('return_msg') is not None else 'unknown',
        }

    except Exception as e:
        print(f'[WeChat] Request error: {e}')
        return {
            'success': False,
            'trade_no': '',
            'qr_code': '',
            'redirect_url': '',
            'error': str(e),
        }


def refund_wechat_order(order_no: str, amount_fen: int, refund_no: str = None) -> Dict[str, Any]:
    """微信退款

    Args:
        order_no: 原订单 out_trade_no
        amount_fen: 退款金额（分），0 表示全额退款
        refund_no: 退款单号，不传自动生成

    Returns:
        {'success': bool, 'refund_no': str, 'error': str}
    """
    import uuid
    cfg = _get_wechat_config()
    app_id = cfg['app_id']
    mch_id = cfg['mch_id']
    api_key = cfg['api_key']

    if not app_id or not mch_id or not api_key:
        # ❌ 旧代码：未配置时返回 mock 成功，导致管理端误以为退款已执行
        print('[WeChat Refund] NOT CONFIGURED — refund rejected, requires manual processing')
        return {
            'success': False,
            'refund_no': '',
            'error': 'WeChat Pay gateway not configured; refund requires manual processing',
        }

    nonce_str = secrets.token_hex(16)
    refund_no = refund_no or f'REF{int(time.time())}{uuid.uuid4().hex[:8].upper()}'

    params = {
        'appid': app_id,
        'mch_id': mch_id,
        'nonce_str': nonce_str,
        'out_trade_no': order_no,
        'out_refund_no': refund_no,
        'total_fee': amount_fen,
        'refund_fee': amount_fen,
    }
    params['sign'] = _sign_wechat(params, api_key)

    xml_body = '<xml>\n' + '\n'.join(f'<{k}>{v}</{k}>' for k, v in params.items()) + '\n</xml>'

    try:
        import urllib.request
        import xml.etree.ElementTree as ET

        # 微信退款需要证书（双向认证），这里先尝试无证书模式
        req = urllib.request.Request(
            'https://api.mch.weixin.qq.com/secapi/pay/refund',
            data=xml_body.encode('utf-8'),
            method='POST',
        )
        req.add_header('Content-Type', 'application/xml')
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()

        root = ET.fromstring(body)
        return_code = root.find('return_code')
        if return_code is not None and return_code.text == 'SUCCESS':
            result_code = root.find('result_code')
            if result_code is not None and result_code.text == 'SUCCESS':
                return {'success': True, 'refund_no': refund_no, 'error': ''}
            err_msg = root.find('err_code_des')
            return {'success': False, 'refund_no': '', 'error': err_msg.text if err_msg is not None else 'refund failed'}
        return {'success': False, 'refund_no': '', 'error': root.find('return_msg').text if root.find('return_msg') is not None else 'unknown'}

    except Exception as e:
        # SSL 错误通常是退款证书未配置。❌ 旧代码静默返回成功，此处改为明确失败并告警
        print(f'[WeChat Refund] Request error (may need client cert): {e}')
        if 'SSL' in str(e).upper():
            return {'success': False, 'refund_no': '', 'error': 'SSL client certificate not configured for refund API'}
        return {'success': False, 'refund_no': '', 'error': str(e)}


def verify_wechat_notify(raw_data: dict, headers: dict) -> Tuple[bool, dict]:
    """验证微信支付回调

    Returns:
        Tuple[bool, dict]: (is_valid, {order_no, trade_no, status})
    """
    cfg = _get_wechat_config()

    from . import _is_gateway_configured
    if not _is_gateway_configured('wechat'):
        # ❌ 旧代码：未配置时直接返回 True（认证绕过风险），此处拒绝所有回调
        print('[WeChat] SECURITY: payment gateway not configured, rejecting callback')
        return False, {}

    # M1: 先检查通信层 return_code（验签之前），失败则直接拒绝
    return_code = raw_data.get('return_code', '')
    if return_code != 'SUCCESS':
        print(f'[WeChat] Communication return_code not success: {return_code}')
        return False, {}

    # 验证签名
    sign = raw_data.get('sign', '')
    verify_params = {k: v for k, v in raw_data.items() if k != 'sign'}
    expected_sign = _sign_wechat(verify_params, cfg['api_key'])

    if sign != expected_sign:
        print('[WeChat] Signature verification failed')
        return False, {}

    result_code = raw_data.get('result_code', '')
    if result_code != 'SUCCESS':
        return False, {}

    # M1: 校验 appid 与 mch_id（签名泄露后的纵深防御）
    if raw_data.get('appid', '') != cfg['app_id']:
        print('[WeChat] SECURITY: appid mismatch in notify')
        return False, {}
    if raw_data.get('mch_id', '') != cfg['mch_id']:
        print('[WeChat] SECURITY: mch_id mismatch in notify')
        return False, {}

    return True, {
        'order_no': raw_data.get('out_trade_no', ''),
        'trade_no': raw_data.get('transaction_id', ''),
        'status': 'paid',
        'total_fee': raw_data.get('total_fee', '0'),   # M1: 供金额校验
    }


# ═══════════════════════════════════════════════════════════════════════════
# 微信支付 V3 API（兼容层 — 供商城/外部模块复用）
# ═══════════════════════════════════════════════════════════════════════════
# V20260812：网关从旧 subscription 插件合流至 payment 插件。
# 保留 shop 依赖的函数签名：call_native_pay / _verify_wechat_sign /
# _decrypt_wechat_resource / refund_order。安全策略与 B 一致：
# 未配置/无证书一律拒绝（fail-closed），严禁 mock 放行。

def _get_wechat_v3_config() -> dict:
    """微信支付 V3 配置（环境变量 → system_config 表兜底）"""
    cfg = {
        'app_id': os.environ.get('WECHAT_APPID', ''),
        'mch_id': os.environ.get('WECHAT_MCHID', ''),
        'api_v3_key': os.environ.get('WECHAT_API_V3_KEY', ''),
        'cert_serial': os.environ.get('WECHAT_CERT_SERIAL', ''),
        'notify_base': os.environ.get('NOTIFY_BASE', ''),
    }
    if not cfg['app_id']:
        from . import _get_config_from_db
        db = _get_config_from_db({
            'app_id': 'wechat_app_id',
            'mch_id': 'wechat_mchid',
            'api_v3_key': 'wechat_api_v3_key',
            'cert_serial': 'wechat_cert_serial',
            'notify_base': 'payment.notify_base',
        })
        for field, value in db.items():
            if not cfg.get(field):
                cfg[field] = value
    return cfg


def _find_wechat_cert(filename: str) -> str:
    """在候选证书目录中查找微信支付证书文件"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, '..', '..', '..', '..', 'certs'),      # 项目根/certs
        os.path.join(here, '..', '..', '..', 'certs'),            # plugins/certs
        os.path.join(here, '..', '..', '..', 'auth-center', 'certs'),
        os.path.join(here, '..', '..', '..', 'auth-center', 'routes', 'certs'),
    ]
    for base in candidates:
        p = os.path.normpath(os.path.join(base, filename))
        if os.path.exists(p):
            return p
    return ''


def _load_wechat_private_key():
    """加载微信商户私钥（apiclient_key.pem）"""
    cert_path = _find_wechat_cert('apiclient_key.pem')
    if not cert_path:
        return None
    from cryptography.hazmat.primitives import serialization
    with open(cert_path, 'rb') as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _rsa_sign_v3(plaintext: str) -> str:
    """微信 V3 RSA-SHA256 签名"""
    import base64
    private_key = _load_wechat_private_key()
    if not private_key:
        return ''
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    sig = private_key.sign(plaintext.encode('utf-8'), asym_padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _generate_v3_nonce() -> str:
    return secrets.token_hex(16)


def _build_v3_auth_header(method: str, url_path: str, body: str = '') -> Dict[str, str]:
    """构造微信支付 API v3 认证头"""
    cfg = _get_wechat_v3_config()
    nonce = _generate_v3_nonce()
    timestamp = str(int(time.time()))
    message = f'{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n'
    signature = _rsa_sign_v3(message)
    return {
        'Authorization': (
            f'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{cfg["mch_id"]}",'
            f'nonce_str="{nonce}",'
            f'signature="{signature}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{cfg["cert_serial"]}"'
        ),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def _decrypt_wechat_resource(resource: dict) -> dict:
    """解密微信支付 V3 API 的 resource 字段（AES-256-GCM）"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        print('[WeChat V3] cryptography not installed, cannot decrypt webhook resource')
        return {}

    cfg = _get_wechat_v3_config()
    api_v3_key = cfg.get('api_v3_key', '')
    if not api_v3_key:
        print('[WeChat V3] SECURITY: api_v3_key not configured, rejecting resource decrypt')
        return {}

    algorithm = resource.get('algorithm', '')
    if algorithm != 'AEAD_AES_256_GCM':
        print(f'[WeChat V3] Unsupported algorithm: {algorithm}')
        return {}

    try:
        import base64
        ciphertext = base64.b64decode(resource.get('ciphertext', ''))
        nonce = base64.b64decode(resource.get('nonce', ''))
        associated_data = resource.get('associated_data', '').encode('utf-8')
        aesgcm = AESGCM(api_v3_key.encode('utf-8'))
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data)
        return json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        print(f'[WeChat V3] Decrypt failed: {e}')
        return {}


def _verify_wechat_sign(headers, body) -> bool:
    """验证微信支付 V3 回调签名（fail-closed）"""
    wechat_sign = headers.get('Wechatpay-Signature', '')
    wechat_timestamp = headers.get('Wechatpay-Timestamp', '')
    wechat_nonce = headers.get('Wechatpay-Nonce', '')

    if not wechat_sign or not wechat_timestamp:
        return False

    cert_path = _find_wechat_cert('wechatpay_cert.pem')
    if not cert_path:
        print('[WeChat V3] SECURITY: platform cert not found, rejecting callback')
        return False

    try:
        import base64
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        message = f'{wechat_timestamp}\n{wechat_nonce}\n{body}\n'
        with open(cert_path, 'rb') as f:
            cert = serialization.load_pem_x509_certificate(f.read())
        public_key = cert.public_key()
        public_key.verify(
            base64.b64decode(wechat_sign),
            message.encode('utf-8'),
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def call_native_pay(order_no: str, description: str, amount_fen: int, notify_url: str = None) -> Dict[str, Any]:
    """微信 V3 Native 扫码下单（商城复用）

    Args:
        order_no: 商户订单号
        description: 商品描述
        amount_fen: 金额（分）
        notify_url: 异步通知 URL，默认使用配置中的 notify_base

    Returns:
        {'stub': bool, 'method': 'wechat', 'code_url': str, 'order_no': str, 'amount': str}
        未配置时返回 {'stub': True, 'error': ...}（禁止 mock 放行）
    """
    cfg = _get_wechat_v3_config()
    if not cfg['app_id'] or not cfg['mch_id'] or not cfg['api_v3_key'] or not cfg['cert_serial']:
        print('[WeChat V3] NOT CONFIGURED — native pay rejected')
        return {'stub': True, 'error': 'WeChat Pay V3 gateway not configured'}

    if not _load_wechat_private_key():
        print('[WeChat V3] MISSING apiclient_key.pem — native pay rejected')
        return {'stub': True, 'error': 'WeChat merchant private key not configured'}

    import urllib.request
    notify_base = cfg['notify_base']
    notify_url = notify_url or (f'{notify_base}/plugin/subscription/api/notify/wechat' if notify_base else '')

    url = 'https://api.mch.weixin.qq.com/v3/pay/transactions/native'
    body = json.dumps({
        'appid': cfg['app_id'],
        'mchid': cfg['mch_id'],
        'description': description,
        'out_trade_no': order_no,
        'notify_url': notify_url,
        'amount': {'total': amount_fen, 'currency': 'CNY'},
    }, ensure_ascii=False)
    headers = _build_v3_auth_header('POST', '/v3/pay/transactions/native', body)
    req = urllib.request.Request(url, data=body.encode('utf-8'), headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        code_url = result.get('code_url', '')
        return {
            'stub': False,
            'method': 'wechat',
            'code_url': code_url,
            'order_no': order_no,
            'amount': f'¥{amount_fen / 100:.2f}',
        }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f'[WeChat V3] Native order failed: {err_body}')
        return {'stub': True, 'error': f'WeChat Order Failed: {err_body}'}
    except Exception as e:
        print(f'[WeChat V3] Native order error: {e}')
        return {'stub': True, 'error': str(e)}


def refund_order(order_no: str, amount_fen: int, refund_no: str = None) -> Dict[str, Any]:
    """微信 V3 退款（商城/订阅通用）— 未配置时拒绝而非 mock 成功"""
    cfg = _get_wechat_v3_config()
    if not cfg['app_id'] or not cfg['mch_id'] or not cfg['api_v3_key'] or not cfg['cert_serial']:
        print('[WeChat V3 Refund] NOT CONFIGURED — refund rejected, requires manual processing')
        return {'success': False, 'refund_no': '', 'error': 'WeChat Pay V3 gateway not configured; refund requires manual processing'}

    if not _load_wechat_private_key():
        return {'success': False, 'refund_no': '', 'error': 'WeChat merchant private key not configured'}

    import uuid
    import urllib.request
    refund_no = refund_no or f'REF{int(time.time())}{uuid.uuid4().hex[:8].upper()}'

    body = json.dumps({
        'out_trade_no': order_no,
        'out_refund_no': refund_no,
        'amount': {'refund': amount_fen, 'total': amount_fen, 'currency': 'CNY'},
    }, ensure_ascii=False)
    url_path = '/v3/refund/domestic/refunds'
    headers = _build_v3_auth_header('POST', url_path, body)
    req = urllib.request.Request(
        f'https://api.mch.weixin.qq.com{url_path}',
        data=body.encode('utf-8'), method='POST', headers=headers,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        if result.get('status') == 'SUCCESS':
            return {'success': True, 'refund_no': refund_no, 'error': ''}
        err_msg = result.get('message', 'refund failed')
        print(f'[WeChat V3 Refund] Failed: {err_msg}')
        return {'success': False, 'refund_no': '', 'error': err_msg}
    except Exception as e:
        print(f'[WeChat V3 Refund] Error (may need client cert): {e}')
        if 'SSL' in str(e).upper():
            return {'success': False, 'refund_no': '', 'error': 'SSL client certificate not configured for refund API'}
        return {'success': False, 'refund_no': '', 'error': str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# 委托扣款（U3 续费引擎依赖 — V20260812 合流）
# 从 auth-center/routes/subscription/gateway/wechat.py 合流 execute_contract_charge，
# 修复其 _is_stub() 假扣款成功漏洞（未配置网关时严禁 mock 放行）。
# ═══════════════════════════════════════════════════════════════════════════

def execute_contract_charge(contract_id: str, order_no: str, amount_fen: int,
                            description: str = '') -> Tuple[bool, str]:
    """执行微信委托扣款（V3 papay transactions）

    Returns:
        Tuple[bool, str]: (success, fail_reason)
    """
    cfg = _get_wechat_v3_config()
    if not cfg['app_id'] or not cfg['mch_id'] or not cfg['api_v3_key'] or not cfg['cert_serial']:
        # fail-closed：A 的旧代码未配置时返回 (True, None) 造成假续费，此处拒绝
        print('[WeChat V3 Charge] NOT CONFIGURED — charge rejected')
        return False, 'WeChat Pay V3 gateway not configured; charge requires manual processing'

    if not _load_wechat_private_key():
        return False, 'WeChat merchant private key not configured'

    if not contract_id:
        return False, 'Missing contract id'

    import urllib.request
    notify_base = cfg['notify_base']
    notify_url = f'{notify_base}/plugin/subscription/api/notify/wechat' if notify_base else ''
    url = 'https://api.mch.weixin.qq.com/v3/papay/transactions'
    body = json.dumps({
        'out_trade_no': order_no,
        'appid': cfg['app_id'],
        'mchid': cfg['mch_id'],
        'description': description,
        'contract_id': contract_id,
        'notify_url': notify_url,
        'amount': {'total': amount_fen, 'currency': 'CNY'},
        'goods_tag': 'subscription_renew',
    }, ensure_ascii=False)
    headers = _build_v3_auth_header('POST', '/v3/papay/transactions', body)
    req = urllib.request.Request(url, data=body.encode(), headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        if 'prepay_id' in result:
            return True, None
        return False, result.get('message', 'Payment failed')
    except urllib.error.HTTPError as e:
        return False, f'Payment failed: {e.read().decode()}'
    except Exception as e:
        return False, str(e)
