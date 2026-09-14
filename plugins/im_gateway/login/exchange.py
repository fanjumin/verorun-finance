#!/usr/bin/env python3
"""第三方登录 — OAuth code→token→userinfo 交换实现（Phase 5，方案 B）。

与 login/providers.py（授权 URL 构造）配套：providers 负责跳转，
本模块负责回调后的 code 交换与用户信息获取，均为标准 OAuth2 授权码流。

仅依赖 Python 标准库（urllib），零新增 pip 依赖。
config 为 login_providers 表该平台配置（client_id / client_secret / scopes）。

统一返回:
    {'openid': str, 'nickname': str, 'avatar': str, 'email': str}
失败抛异常（消息为面向用户的中文提示，由路由捕获后友好展示）。
"""
import json
import urllib.parse
import urllib.request

_TIMEOUT = 10


class OAuthExchangeError(Exception):
    """OAuth 交换失败（含平台 API 拒绝），消息可直接面向用户展示。"""


def _post_json(url, data=None, headers=None):
    """POST 并解析 JSON；非 2xx 抛 OAuthExchangeError。"""
    body = urllib.parse.urlencode(data or {}).encode() if data else b''
    req = urllib.request.Request(url, data=body, method='POST')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            detail = ''
        raise OAuthExchangeError(f'API error {e.code}: {detail}')
    except urllib.error.URLError as e:
        raise OAuthExchangeError(f'Network error: {e.reason}')
    except (json.JSONDecodeError, ValueError):
        raise OAuthExchangeError('Invalid API response')


def _get_json(url, headers=None):
    """GET 并解析 JSON；非 2xx 抛 OAuthExchangeError。"""
    req = urllib.request.Request(url, method='GET')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8')[:300]
        except Exception:
            detail = ''
        raise OAuthExchangeError(f'API error {e.code}: {detail}')
    except urllib.error.URLError as e:
        raise OAuthExchangeError(f'Network error: {e.reason}')
    except (json.JSONDecodeError, ValueError):
        raise OAuthExchangeError('Invalid API response')


def _require_config(config, *keys):
    """检查配置字段，缺失抛 OAuthExchangeError。"""
    for k in keys:
        if not (config or {}).get(k):
            raise OAuthExchangeError(f'Missing {k} in provider config')


# ── 各平台实现 ──

def _wechat(code, redirect_uri, config):
    """微信开放平台扫码（snsapi_login）→ sns/oauth2/access_token → sns/userinfo。"""
    _require_config(config, 'client_id', 'client_secret')
    token = _post_json('https://api.weixin.qq.com/sns/oauth2/access_token', {
        'appid': config['client_id'],
        'secret': config['client_secret'],
        'code': code,
        'grant_type': 'authorization_code',
    })
    if token.get('errcode'):
        raise OAuthExchangeError(f"WeChat: {token.get('errmsg', 'code exchange failed')}")
    openid = token.get('openid', '')
    if not openid:
        raise OAuthExchangeError('WeChat: openid missing')
    info = _get_json('https://api.weixin.qq.com/sns/userinfo?'
                     + urllib.parse.urlencode({
                         'access_token': token.get('access_token', ''),
                         'openid': openid,
                         'lang': 'zh_CN',
                     }))
    if info.get('errcode'):
        raise OAuthExchangeError(f"WeChat: {info.get('errmsg', 'userinfo failed')}")
    return {
        'openid': openid,
        'nickname': info.get('nickname', ''),
        'avatar': info.get('headimgurl', ''),
        'email': '',
    }


def _qq(code, redirect_uri, config):
    """QQ 互联 OAuth2 → oauth2.0/token（表单文本）→ me（JSONP）→ user/get_user_info。"""
    _require_config(config, 'client_id', 'client_secret')
    token_url = 'https://graph.qq.com/oauth2.0/token?' + urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'client_id': config['client_id'],
        'client_secret': config['client_secret'],
        'code': code,
        'redirect_uri': redirect_uri,
    })
    try:
        with urllib.request.urlopen(token_url, timeout=_TIMEOUT) as resp:
            token_text = resp.read().decode('utf-8')
    except urllib.error.URLError as e:
        raise OAuthExchangeError(f'Network error: {e.reason}')
    params = urllib.parse.parse_qs(token_text)
    access_token = (params.get('access_token') or [''])[0]
    if not access_token:
        raise OAuthExchangeError(f"QQ: token exchange failed ({token_text[:200]})")
    # openid：/oauth2.0/me 返回 JSONP 包裹的 JSON
    try:
        with urllib.request.urlopen(
                'https://graph.qq.com/oauth2.0/me?access_token=' + access_token,
                timeout=_TIMEOUT) as resp:
            me_text = resp.read().decode('utf-8')
    except urllib.error.URLError as e:
        raise OAuthExchangeError(f'Network error: {e.reason}')
    openid = ''
    start = me_text.find('{')
    end = me_text.rfind('}')
    if start >= 0 and end > start:
        try:
            openid = json.loads(me_text[start:end + 1]).get('openid', '')
        except (json.JSONDecodeError, ValueError):
            openid = ''
    if not openid:
        raise OAuthExchangeError(f'QQ: openid missing ({me_text[:200]})')
    info = _get_json('https://graph.qq.com/user/get_user_info?' + urllib.parse.urlencode({
        'access_token': access_token,
        'oauth_consumer_key': config['client_id'],
        'openid': openid,
    }))
    if info.get('ret') not in (None, 0):
        raise OAuthExchangeError(f"QQ: {info.get('msg', 'userinfo failed')}")
    return {
        'openid': openid,
        'nickname': info.get('nickname', ''),
        'avatar': info.get('figureurl_qq_2') or info.get('figureurl_qq_1', ''),
        'email': '',
    }


def _weibo(code, redirect_uri, config):
    """微博 OAuth2 → oauth2/access_token → users/show.json。"""
    _require_config(config, 'client_id', 'client_secret')
    token = _post_json('https://api.weibo.com/oauth2/access_token', {
        'client_id': config['client_id'],
        'client_secret': config['client_secret'],
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
    })
    access_token = token.get('access_token', '')
    uid = token.get('uid', '')
    if not access_token or not uid:
        raise OAuthExchangeError(f"Weibo: token exchange failed ({token.get('error_description', '')})")
    info = _get_json('https://api.weibo.com/2/users/show.json?' + urllib.parse.urlencode({
        'access_token': access_token,
        'uid': uid,
    }))
    if info.get('error'):
        raise OAuthExchangeError(f"Weibo: {info.get('error', 'userinfo failed')}")
    return {
        'openid': str(uid),
        'nickname': info.get('screen_name') or info.get('name', ''),
        'avatar': info.get('avatar_large', ''),
        'email': '',
    }


def _github(code, redirect_uri, config):
    """GitHub OAuth2 → login/oauth/access_token → api.github.com/user。"""
    _require_config(config, 'client_id', 'client_secret')
    token = _post_json('https://github.com/login/oauth/access_token', {
        'client_id': config['client_id'],
        'client_secret': config['client_secret'],
        'code': code,
        'redirect_uri': redirect_uri,
    }, headers={'Accept': 'application/json'})
    access_token = token.get('access_token', '')
    if not access_token:
        raise OAuthExchangeError(f"GitHub: {token.get('error_description', token.get('error', 'token exchange failed'))}")
    info = _get_json('https://api.github.com/user',
                     headers={'Authorization': f'Bearer {access_token}',
                              'Accept': 'application/vnd.github+json',
                              'User-Agent': 'VeroRun-IMGateway'})
    if info.get('message') and 'id' not in info:
        raise OAuthExchangeError(f"GitHub: {info.get('message', 'userinfo failed')}")
    return {
        'openid': str(info.get('id', '')),
        'nickname': info.get('name') or info.get('login', ''),
        'avatar': info.get('avatar_url', ''),
        'email': info.get('email', ''),
    }


def _google(code, redirect_uri, config):
    """Google OAuth2 → oauth2.googleapis.com/token → oauth2/v3/userinfo。"""
    _require_config(config, 'client_id', 'client_secret')
    token = _post_json('https://oauth2.googleapis.com/token', {
        'client_id': config['client_id'],
        'client_secret': config['client_secret'],
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': redirect_uri,
    })
    access_token = token.get('access_token', '')
    if not access_token:
        raise OAuthExchangeError(f"Google: {token.get('error_description', token.get('error', 'token exchange failed'))}")
    info = _get_json('https://www.googleapis.com/oauth2/v3/userinfo',
                     headers={'Authorization': f'Bearer {access_token}'})
    if info.get('error'):
        raise OAuthExchangeError(f"Google: {info.get('error_description', info.get('error', 'userinfo failed'))}")
    return {
        'openid': info.get('sub', ''),
        'nickname': info.get('name', ''),
        'avatar': info.get('picture', ''),
        'email': info.get('email', ''),
    }


# 注册表
_EXCHANGERS = {
    'wechat': _wechat,
    'qq': _qq,
    'weibo': _weibo,
    'github': _github,
    'google': _google,
}


def exchange_and_get_userinfo(provider: str, code: str, redirect_uri: str, config: dict) -> dict:
    """按 provider 完成 code 交换并返回用户信息（openid/nickname/avatar/email）。"""
    fn = _EXCHANGERS.get(provider)
    if fn is None:
        raise OAuthExchangeError(f'Unsupported provider: {provider}')
    return fn(code, redirect_uri, config or {})
