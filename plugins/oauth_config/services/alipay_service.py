#!/usr/bin/env python3
"""Alipay OAuth Service — 支付宝登录 via Alipay Open Platform.
   Supports stub mode (no credentials), production mode, and multi-tenant config from DB.

配置优先级：
1. DB 表 oauth_providers → 按 site_domain + provider='alipay' 查询
2. 环境变量 ALIPAY_APP_ID / ALIPAY_PRIVATE_KEY（全局兜底）
"""
from i18n import _
import os, json, urllib.request, urllib.parse, time, base64, traceback

ALIPAY_APP_ID = os.environ.get('ALIPAY_APP_ID', '')
ALIPAY_PRIVATE_KEY = os.environ.get('ALIPAY_PRIVATE_KEY', '')
ALIPAY_GATEWAY = 'https://openapi.alipay.com/gateway.do'


def _log(msg):
    """Log to plugin logger channel (§10.5)."""
    try:
        from plugins.oauth_config import _plugin_log
        _plugin_log(msg)
    except Exception:
        pass  # silently ignore logging failures to avoid breaking OAuth flow


def _get_config(site_domain=None, provider='alipay'):
    """获取站点 OAuth 配置，按优先级：DB oauth_providers > 环境变量"""
    # 1. 从 DB 按域名查 oauth_providers
    if site_domain:
        try:
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    'SELECT client_key, client_secret FROM oauth_providers '
                    'WHERE site_domain=%s AND provider=%s AND is_active=1',
                    (site_domain, provider)
                ).fetchone()
            if row:
                return dict(row)
        except Exception:
            pass

    # 2. 环境变量兜底
    if ALIPAY_APP_ID and ALIPAY_APP_ID != 'stub':
        return {'client_key': ALIPAY_APP_ID, 'client_secret': ALIPAY_PRIVATE_KEY}

    return None


def is_stub(site_domain=None):
    """检查是否 stub 模式（未配置）"""
    cfg = _get_config(site_domain)
    return cfg is None


def _get_alipay_callback(domain=None):
    """动态生成支付宝回调 URL，支持多租户"""
    if domain:
        return f'https://{domain}/auth/alipay/callback'
    fallback_domain = os.environ.get('DEPLOY_DOMAIN', '')
    return f'https://{fallback_domain}/auth/alipay/callback'


def _ensure_pem_format(key_str: str, key_type: str = 'PRIVATE KEY') -> str:
    """确保密钥字符串有正确的 PEM 头尾标记。"""
    key_str = key_str.strip()
    begin_marker = f'-----BEGIN {key_type}-----'
    end_marker = f'-----END {key_type}-----'
    if not key_str.startswith('-----BEGIN '):
        key_str = begin_marker + '\n' + key_str + '\n' + end_marker
    return key_str


def _sign_params(params: dict, private_key: str) -> str:
    """RSA2 签名：对参数按 key 排序后签名。"""
    private_key = _ensure_pem_format(private_key, 'PRIVATE KEY')

    sorted_keys = sorted(params.keys())
    sign_str = '&'.join([f'{k}={params[k]}' for k in sorted_keys])

    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

    key = serialization.load_pem_private_key(private_key.encode('utf-8'), password=None)
    sig = key.sign(
        sign_str.encode('utf-8'),
        asym_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _api_call(method: str, biz_content: dict, app_id: str, private_key: str) -> dict:
    """通用支付宝 API 调用（RSA2 签名 POST 请求）。

    与 payment_service.py 一致的做法：
      - 所有参数（含 sign）放在 POST body 中
      - Content-Type: application/x-www-form-urlencoded
      - 已通过 payment_service.query_payment_status 验证可行
    """
    biz_content_str = json.dumps(biz_content, ensure_ascii=False)

    params = {
        'app_id': app_id,
        'method': method,
        'format': 'JSON',
        'charset': 'utf-8',
        'sign_type': 'RSA2',
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'version': '1.0',
        'biz_content': biz_content_str,
    }
    sign = _sign_params(params, private_key)
    params['sign'] = sign

    # 所有参数（含 biz_content 和 sign）放在 POST body 中
    data_parts = []
    for k, v in params.items():
        data_parts.append(f'{k}={urllib.parse.quote(str(v))}')
    data_bytes = '&'.join(data_parts).encode('utf-8')

    req = urllib.request.Request(ALIPAY_GATEWAY, data=data_bytes)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    req.add_header('Accept-Encoding', 'identity')
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read()
    content_encoding = resp.headers.get('Content-Encoding', '')
    if content_encoding and 'gzip' in content_encoding:
        import gzip as _gz
        raw = _gz.decompress(raw)
    import sys as _sys
    _log(f'[alipay _api_call] response body[:200]={raw[:200]}')
    _sys.stdout.flush()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('gbk')
    result = json.loads(text)
    return result


def get_oauth_url(site_domain=None, redirect_to=''):
    """生成支付宝授权登录 URL"""
    cfg = _get_config(site_domain)
    if not cfg:
        return None

    app_id = cfg['client_key']
    callback = _get_alipay_callback(site_domain)

    # 支付宝 OAuth 2.0 授权 URL（无需签名，浏览器直接跳转）
    base_url = 'https://openauth.alipay.com/oauth2/publicAppAuthorize.htm'

    params = {
        'app_id': app_id,
        'scope': 'auth_user',
        'redirect_uri': callback,
    }

    if redirect_to:
        params['state'] = redirect_to

    return base_url + '?' + urllib.parse.urlencode(params)


def get_access_token(code, site_domain=None):
    """使用授权码换取 access_token（RSA2 签名 POST 请求）"""
    cfg = _get_config(site_domain)
    if not cfg:
        return {'error': 'stub mode', 'stub_user_id': 'stub_alipay_' + code[:8]}

    app_id = cfg['client_key']
    private_key = cfg['client_secret']

    if not private_key:
        return {'error': _('Alipay private key not configured')}

    try:
        # alipay.system.oauth.token 不使用 biz_content
        # grant_type 和 code 是顶级参数（和 biz_content 类 API 不同）
        params = {
            'app_id': app_id,
            'method': 'alipay.system.oauth.token',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'grant_type': 'authorization_code',
            'code': code,
        }
        sign = _sign_params(params, private_key)
        params['sign'] = sign

        data_parts = []
        for k, v in params.items():
            data_parts.append(f'{k}={urllib.parse.quote(str(v))}')
        data_bytes = '&'.join(data_parts).encode('utf-8')

        req = urllib.request.Request(ALIPAY_GATEWAY, data=data_bytes)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read()
        text = raw.decode('utf-8', errors='replace')
        result = json.loads(text)
        token_resp = result.get('alipay_system_oauth_token_response', {})

        # 错误响应在 error_response 中，不在 alipay_system_oauth_token_response
        if not token_resp:
            error_resp = result.get('error_response', {})
            sub = error_resp.get('sub_msg', '')
            msg = error_resp.get('msg', '')
            err_msg = sub or msg or _('Alipay API Error')
            _log(f'[alipay get_access_token error_response] {err_msg}')
            import sys as _sys; _sys.stdout.flush()
            return {'error': err_msg}

        if token_resp.get('code') != '10000' and 'access_token' not in token_resp:
            err_msg = token_resp.get('sub_msg', token_resp.get('msg', _('Alipay API Error')))
            _log(f'[alipay get_access_token token_error] {err_msg}')
            import sys as _sys; _sys.stdout.flush()
            return {'error': err_msg}

        return {
            'access_token': token_resp.get('access_token'),
            'user_id': token_resp.get('user_id'),
            'expires_in': token_resp.get('expires_in', 3600),
            'refresh_token': token_resp.get('refresh_token', ''),
        }
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        err_msg = f'{e} | TB: {_tb.format_exc()[:200]}'
        _log(f'[alipay get_access_token EXCEPTION] {err_msg}')
        return {'error': str(e)}


def get_user_info(access_token, site_domain=None):
    """获取支付宝用户信息（RSA2 签名 POST 请求）"""
    cfg = _get_config(site_domain)
    if not cfg:
        return {
            'nickname': _('Alipay User_stub'),
            'avatar': '',
            'user_id': 'stub_alipay_user',
        }

    app_id = cfg['client_key']
    private_key = cfg['client_secret']

    if not private_key:
        return {'error': _('Alipay private key not configured')}

    try:
        # alipay.user.info.share 使用 auth_token 而非 biz_content
        params = {
            'app_id': app_id,
            'method': 'alipay.user.info.share',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'auth_token': access_token,
        }
        sign = _sign_params(params, private_key)
        params['sign'] = sign

        data_bytes = '&'.join(
            [f'{k}={urllib.parse.quote(str(params[k]))}' for k in params]
        ).encode('utf-8')

        req = urllib.request.Request(ALIPAY_GATEWAY, data=data_bytes)
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode('utf-8'))

        user_data = body.get('alipay_user_info_share_response', {})
        if user_data.get('code') != '10000' and 'user_id' not in user_data:
            return {'error': user_data.get('sub_msg', user_data.get('msg', _('Failed to Get User Info')))}

        return {
            'nickname': user_data.get('nick_name', ''),
            'avatar': user_data.get('avatar', ''),
            'user_id': user_data.get('user_id', ''),
            'province': user_data.get('province', ''),
            'city': user_data.get('city', ''),
        }
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        _log(f'[alipay get_user_info EXCEPTION] {e}')
        return {'error': str(e)}
