#!/usr/bin/env python3
"""Douyin OAuth Service — 支持多租户按域名配置，兼容全局环境变量

配置优先级：
1. DB 表 oauth_providers → 按 site_domain + provider 查询
2. 环境变量 DOUYIN_CLIENT_KEY / DOUYIN_CLIENT_SECRET（全局兜底）
"""
from i18n import _
import os, json, urllib.request, urllib.parse


def _get_config(site_domain=None, provider='douyin'):
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
    key = os.environ.get('DOUYIN_CLIENT_KEY', '')
    secret = os.environ.get('DOUYIN_CLIENT_SECRET', '')
    if key and key != 'stub':
        return {'client_key': key, 'client_secret': secret}

    return None


def is_stub(site_domain=None):
    """检查是否 stub 模式（未配置）"""
    cfg = _get_config(site_domain)
    return cfg is None


def get_oauth_url(site_domain=None, state='login'):
    """生成抖音扫码登录 URL，按域名选择凭证"""
    cfg = _get_config(site_domain)
    if not cfg:
        return None

    # 回调地址：根据域名自动生成
    domain = site_domain or os.environ.get('DOUYIN_CALLBACK_DOMAIN', os.environ.get('DEPLOY_DOMAIN', ''))
    callback = f'https://{domain}/auth/douyin/callback'
    encoded = urllib.parse.quote(callback)

    return (f'https://open.douyin.com/platform/oauth/connect?'
            f'client_key={cfg["client_key"]}'
            f'&response_type=code'
            f'&scope=user_info'
            f'&redirect_uri={encoded}'
            f'&state={state}')


def get_access_token(code, site_domain=None):
    """用授权码换 access_token + open_id，按域名选择凭证"""
    cfg = _get_config(site_domain)
    if not cfg:
        return {'error': _('TikTok Login Not Configured'), 'open_id': 'stub_open_' + code[:8],
                'union_id': 'stub_union', 'access_token': 'stub_token',
                'refresh_token': 'stub_refresh', 'expires_in': 86400}

    data = urllib.parse.urlencode({
        'client_key': cfg['client_key'],
        'client_secret': cfg['client_secret'],
        'code': code,
        'grant_type': 'authorization_code',
    }).encode()

    req = urllib.request.Request(
        'https://open.douyin.com/passport/open/access_token/',
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())
        if body.get('data') and body['data'].get('open_id'):
            d = body['data']
            return {
                'open_id': d['open_id'],
                'union_id': d.get('union_id', ''),
                'access_token': d['access_token'],
                'refresh_token': d.get('refresh_token', ''),
                'expires_in': d.get('expires_in', 0),
            }
        msg = body.get('message', _('TikTok API Error'))
        err = body.get('data', {}).get('description', msg)
        return {'error': err}
    except Exception as e:
        return {'error': str(e)}


def get_user_info(open_id, access_token, site_domain=None):
    """获取用户昵称和头像"""
    if is_stub(site_domain):
        return {'nickname': _('TikTok User_') + open_id[-4:], 'avatar': '',
                'open_id': open_id, 'union_id': 'stub_union'}
    url = ('https://open.douyin.com/oauth/userinfo?'
           + urllib.parse.urlencode({'access_token': access_token, 'open_id': open_id}))
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        body = json.loads(resp.read())
        d = body.get('data', {})
        if d.get('open_id'):
            return {
                'nickname': d.get('nickname', ''),
                'avatar': d.get('avatar', ''),
                'open_id': d['open_id'],
                'union_id': d.get('union_id', ''),
            }
        return {'error': d.get('description', body.get('message', _('Failed to Get User Info')))}
    except Exception as e:
        return {'error': str(e)}


def save_config_to_db(site_domain, client_key, client_secret, provider='douyin'):
    """在管理后台保存站点 OAuth 配置到 DB"""
    from models import get_db
    from datetime import datetime
    now = datetime.now().isoformat()
    with get_db() as conn:
        existing = conn.execute(
            'SELECT id FROM oauth_providers WHERE site_domain=%s AND provider=%s',
            (site_domain, provider)
        ).fetchone()
        if existing:
            conn.execute(
                'UPDATE oauth_providers SET client_key=%s, client_secret=%s, is_active=1, updated_at=%s WHERE id=%s',
                (client_key, client_secret, now, existing['id'])
            )
        else:
            conn.execute(
                'INSERT INTO oauth_providers (site_domain, provider, client_key, client_secret, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (site_domain, provider, client_key, client_secret, now, now)
            )
        conn.commit()
    return True


def list_configs(provider='douyin'):
    """列出所有站点 OAuth 配置"""
    from models import get_db
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, site_domain, client_key, is_active, created_at, updated_at '
            'FROM oauth_providers WHERE provider=%s ORDER BY site_domain',
            (provider,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_config(config_id):
    """删除站点 OAuth 配置"""
    from models import get_db
    with get_db() as conn:
        conn.execute('DELETE FROM oauth_providers WHERE id=%s', (config_id,))
        conn.commit()
    return True


# =============================================
# 小程序端 OAuth（code2session）
# =============================================

def get_miniprogram_config(site_domain=None):
    """获取小程序 OAuth 配置"""
    # 1. 从 DB 按域名查
    if site_domain:
        try:
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    'SELECT client_key, client_secret FROM oauth_providers '
                    'WHERE site_domain=%s AND provider=%s AND is_active=1',
                    (site_domain, 'douyin_miniprogram')
                ).fetchone()
            if row:
                return dict(row)
        except Exception:
            pass

    # 2. 环境变量兜底
    key = os.environ.get('DOUYIN_MINI_APPID', '')
    secret = os.environ.get('DOUYIN_MINI_SECRET', '')
    if key and key != 'stub':
        return {'client_key': key, 'client_secret': secret}

    return None


def miniprogram_is_stub(site_domain=None):
    """检查小程序是否 stub 模式（未配置）"""
    cfg = get_miniprogram_config(site_domain)
    return cfg is None


def code2session(code, site_domain=None):
    """抖音小程序 code2session：用 code 换取 openid/session_key
    
    抖音小程序 OAuth 流程：
    1. 前端调用 tt.login() 获取 code
    2. 后端调用此接口用 code 换取 openid
    3. 根据 openid 查找/创建用户
    """
    cfg = get_miniprogram_config(site_domain)
    if not cfg:
        # Stub 模式：模拟成功
        return {
            'openid': 'stub_mp_' + code[:12] if code else 'stub_mp_000000',
            'session_key': 'stub_session_key_' + code[:8] if code else 'stub_key',
            'unionid': '',
            'error': None
        }

    # 调用抖音 API
    data = urllib.parse.urlencode({
        'appid': cfg['client_key'],
        'secret': cfg['client_secret'],
        'code': code,
        'grant_type': 'authorization_code',
    }).encode()

    req = urllib.request.Request(
        'https://open.douyin.com/v2/mp/userinfo/code2session',
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())
        data = body.get('data', {})
        if data.get('openid'):
            return {
                'openid': data['openid'],
                'session_key': data.get('session_key', ''),
                'unionid': data.get('unionid', ''),
                'error': None
            }
        msg = data.get('description', body.get('message', _('TikTok API Error')))
        return {'openid': None, 'session_key': None, 'unionid': None, 'error': msg}
    except Exception as e:
        return {'openid': None, 'session_key': None, 'unionid': None, 'error': str(e)}
