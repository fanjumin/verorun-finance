#!/usr/bin/env python3
"""WeChat OAuth Service — QR scan login via WeChat Open Platform (开放平台).
   Supports stub mode (no credentials), production mode, and multi-tenant config from DB.

配置优先级：
1. DB 表 oauth_providers → 按 site_domain + provider='wechat' 查询
2. 环境变量 WECHAT_APP_ID / WECHAT_APP_SECRET（全局兜底）
"""
from i18n import _
import os, json, urllib.request, urllib.parse

WECHAT_APP_ID = os.environ.get('WECHAT_APP_ID', '')
WECHAT_APP_SECRET = os.environ.get('WECHAT_APP_SECRET', '')


def _get_config(site_domain=None, provider='wechat'):
    """获取站点 OAuth 配置，按优先级：DB > 环境变量"""
    # 1. 从 DB 按域名查
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
    if WECHAT_APP_ID and WECHAT_APP_ID != 'stub':
        return {'client_key': WECHAT_APP_ID, 'client_secret': WECHAT_APP_SECRET}

    return None


def is_stub(site_domain=None):
    """检查是否 stub 模式（未配置）"""
    cfg = _get_config(site_domain)
    return cfg is None


def _get_wechat_callback(domain=None):
    """动态生成微信回调 URL，支持多租户"""
    if domain:
        return f'https://{domain}/auth/wechat/callback'
    # fallback: 使用默认 domain
    fallback_domain = os.environ.get('DEPLOY_DOMAIN', '')
    return f'https://{fallback_domain}/auth/wechat/callback'


def get_qr_url(site_domain=None):
    """Generate WeChat QR scan login URL (open.weixin.qq.com)."""
    cfg = _get_config(site_domain)
    if not cfg:
        return None
    
    app_id = cfg['client_key']
    callback = _get_wechat_callback(site_domain)
    encoded = urllib.parse.quote(callback)
    
    return (f'https://open.weixin.qq.com/connect/qrconnect?'
            f'appid={app_id}'
            f'&redirect_uri={encoded}'
            f'&response_type=code'
            f'&scope=snsapi_login'
            f'&state=login#wechat_redirect')


def get_openid_by_code(code, site_domain=None):
    """Exchange authorization code for access_token, openid, and unionid."""
    cfg = _get_config(site_domain)
    if not cfg:
        return {
            'openid': 'stub_open_' + code[:8],
            'unionid': 'stub_union',
            'access_token': 'stub_token',
            'refresh_token': 'stub_refresh',
            'expires_in': 7200,
        }
    
    app_id = cfg['client_key']
    secret = cfg['client_secret']
    
    url = 'https://api.weixin.qq.com/sns/oauth2/access_token'
    params = {
        'appid': app_id,
        'secret': secret,
        'code': code,
        'grant_type': 'authorization_code',
    }
    full_url = url + '?' + urllib.parse.urlencode(params)
    try:
        resp = urllib.request.urlopen(full_url, timeout=10)
        body = json.loads(resp.read())
        if 'openid' in body:
            return body
        return {'error': body.get('errmsg', _('WeChat API Error'))}
    except Exception as e:
        return {'error': str(e)}


def get_user_info(openid, access_token):
    """Get WeChat user nickname and avatar."""
    # stub 模式检查（无 site_domain 时用全局配置）
    if not WECHAT_APP_ID or WECHAT_APP_ID == 'stub':
        return {
            'nickname': _('WeChat User_') + openid[-4:],
            'avatar': '',
            'openid': openid,
            'unionid': 'stub_union',
        }
    url = 'https://api.weixin.qq.com/sns/userinfo'
    params = {
        'access_token': access_token,
        'openid': openid,
        'lang': 'zh_CN',
    }
    full_url = url + '?' + urllib.parse.urlencode(params)
    try:
        resp = urllib.request.urlopen(full_url, timeout=10)
        body = json.loads(resp.read())
        if 'nickname' in body:
            return {
                'nickname': body.get('nickname', ''),
                'avatar': body.get('headimgurl', ''),
                'openid': body['openid'],
                'unionid': body.get('unionid', ''),
            }
        return {'error': body.get('errmsg', _('Failed to Get User Info'))}
    except Exception as e:
        return {'error': str(e)}
