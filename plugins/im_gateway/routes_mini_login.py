#!/usr/bin/env python3
"""IM Gateway — Mini-Program 登录蓝图（统一登录侧）

v2.1.0：小程序平台登录（douyin / wechat / telegram / line）从
plugins/mini_app_builder 迁入 IM Gateway，实现「所有登录侧走 IM Gateway」。
登录闭环仍复用 auth-center 服务层（user_registry 注册 + jwt_service 签发）——
IM Gateway 是登录入口，auth-center 是登录内核（全站唯一签发通道）。

Blueprint 前缀 /api/v1/mini-program/auth（长于 mini_app_builder 的
/api/v1/mini-program），门卫按最长前缀匹配，二者互不干扰；登录端点路径
与旧版完全一致，SDK / 生成模板零改动，已上线小程序无需重生成。
"""

import hashlib
import hmac
import json
import os
import sys
from urllib.parse import parse_qs, unquote

from flask import Blueprint, jsonify, request

# Blueprint（公开，无需管理员鉴权 —— 小程序端用户登录）
mini_login_bp = Blueprint('im_gateway_mini_login', __name__,
                          url_prefix='/api/v1/mini-program/auth')


def _ensure_auth_center():
    """Ensure auth-center is importable (main_site removes it from sys.path
    after startup; user_registry is not cached in sys.modules)."""
    _auth_center = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
    if os.path.isdir(_auth_center) and _auth_center not in sys.path:
        sys.path.insert(0, _auth_center)


def _ok(data=None):
    return jsonify({'success': True, 'data': data})


def _err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


def _issue_platform_jwt(user_id):
    """统一登录签发（与 routes_third_login / 主站同通道：session_service）。

    services.jwt_service 无 generate_token，历史代码引用了不存在的函数，
    导致所有平台登录 500；此处收敛到全站唯一签发入口。
    平台用户非管理员：is_admin 走默认 False。
    返回 JWT 字符串（签发失败抛异常由调用方兜底为 500）。
    """
    from services.session_service import issue_auth_session
    result = issue_auth_session(user_id, '', app_name='main')
    return result['token']


def _require_auth():
    """Require valid JWT token, return (user_id, error_response)"""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, _err('Invalid or expired token', 401)
    token = auth.replace('Bearer ', '')
    try:
        _ensure_auth_center()
        from services.jwt_service import validate_token
        payload = validate_token(token)
        user_id = payload.get('user_id') if payload else None
    except Exception:
        user_id = None
    if not user_id:
        return None, _err('Invalid or expired token', 401)
    return user_id, None


# ═══════════════════════════════════════════════════════════════
# Auth endpoints
# ═══════════════════════════════════════════════════════════════

@mini_login_bp.route('/login', methods=['POST'])
def mp_auth_login():
    """Platform login — exchange platform credentials for system JWT.

    Request body:
        {
            "platform": "douyin" | "wechat" | "telegram" | "line",
            "code": "..."           (Douyin/WeChat: tt.login()/wx.login() code)
            "initData": "..."       (Telegram: WebApp.initData)
            "accessToken": "...",   (LINE: liff.getAccessToken())
            "nickname": "...",
            "avatar": "..."
        }

    Response:
        {
            "success": true,
            "data": {
                "token": "eyJ...",
                "user": { "id": ..., "username": ..., "display_name": ...,
                          "platform": "...", "platform_user_id": "...",
                          "is_new_user": false }
            }
        }
    """
    data = request.get_json(force=True, silent=True) or {}
    platform = data.get('platform', '')

    if platform == 'douyin':
        return _douyin_login(data)
    elif platform == 'wechat':
        return _wechat_login(data)
    elif platform == 'telegram':
        return _telegram_login(data)
    elif platform == 'line':
        return _line_login(data)
    else:
        return _err(f'Unsupported platform: {platform}', 400)


@mini_login_bp.route('/validate', methods=['POST'])
def mp_auth_validate():
    """Validate a JWT token"""
    user_id, err = _require_auth()
    if err:
        return err
    return _ok({'valid': True, 'user_id': user_id})


# ═══════════════════════════════════════════════════════════════
# Platform login implementations
# ═══════════════════════════════════════════════════════════════

def _douyin_login(data):
    """Handle Douyin mini-program login — code2session → auth-center registry."""
    code = data.get('code', '')
    if not code:
        return _err('code is required', 400)

    try:
        _ensure_auth_center()
        try:
            from plugins.oauth_config.services.douyin_service import code2session
        except ImportError:
            code2session = None

        domain = (request.headers.get('Host', '') or '').split(':')[0]
        if domain.startswith('www.'):
            domain = domain[4:]

        # P0-3 fail-closed：oauth_config stub 模式下 code2session 会伪造 openid，
        # 登录网关必须拒绝，否则任意 code 皆可换取系统 JWT。
        try:
            from plugins.oauth_config.services.douyin_service import miniprogram_is_stub
            if miniprogram_is_stub(site_domain=domain):
                return _err('Douyin mini-program credentials not configured for this site', 503)
        except ImportError:
            pass

        result = code2session(code, site_domain=domain) if code2session else None
        if not result or not result.get('openid'):
            return _err('Failed to exchange code with Douyin', 400)

        openid = result['openid']
        if openid.startswith('stub_'):
            return _err('Douyin mini-program credentials not configured for this site', 503)
        nickname = data.get('nickname', '') or ''
        avatar = data.get('avatar', '') or ''
        username = 'dy_' + hashlib.md5(openid.encode()).hexdigest()[:12]
        display_name = nickname or f'DouyinUser_{openid[-6:]}'

        from services.user_registry import register_or_get_platform_user
        user = register_or_get_platform_user(
            'douyin', openid, username, display_name, avatar)

        from plugins.mini_app_builder.platform_users import upsert_mapping
        upsert_mapping('douyin', openid, user['id'], username, display_name, avatar)

        token = _issue_platform_jwt(user['id'])

        return _ok({
            'token': token,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'display_name': user.get('display_name', ''),
                'platform': 'douyin',
                'platform_user_id': openid,
                'is_new_user': False,
            }
        })
    except Exception as e:
        import logging
        logging.error(f'[IMGateway] Douyin login failed: {e}')
        return _err(f'Login failed: {e}', 500)


def _wechat_login(data):
    """Handle WeChat mini-program login — openid via oauth_config → auth-center."""
    code = data.get('code', '')
    if not code:
        return _err('code is required', 400)

    try:
        _ensure_auth_center()
        try:
            from plugins.oauth_config.services.wechat_service import get_openid_by_code
        except ImportError:
            get_openid_by_code = None

        domain = (request.headers.get('Host', '') or '').split(':')[0]
        if domain.startswith('www.'):
            domain = domain[4:]

        # P0-3 fail-closed：stub 模式下 get_openid_by_code 返回 stub_open_/stub_union
        # 假身份（unionid 恒定还导致所有伪造者共享同一账号），网关侧必须拒绝。
        try:
            from plugins.oauth_config.services.wechat_service import is_stub
            if is_stub(site_domain=domain):
                return _err('WeChat credentials not configured for this site', 503)
        except ImportError:
            pass

        session_info = get_openid_by_code(code, site_domain=domain) if get_openid_by_code else None
        if not session_info or not session_info.get('openid'):
            return _err('Failed to exchange code with WeChat', 400)

        openid = session_info.get('openid', '')
        unionid = session_info.get('unionid', openid)
        if openid.startswith('stub_') or str(unionid).startswith('stub_'):
            return _err('WeChat credentials not configured for this site', 503)

        username = 'wx_' + hashlib.md5(openid.encode()).hexdigest()[:12]
        nickname = data.get('nickname', '') or 'WeChat User'
        avatar = data.get('avatar', '') or ''

        from services.user_registry import register_or_get_platform_user
        user = register_or_get_platform_user(
            'wechat', unionid, username, nickname, avatar)

        from plugins.mini_app_builder.platform_users import upsert_mapping
        upsert_mapping('wechat', unionid, user['id'], username, nickname, avatar)

        token = _issue_platform_jwt(user['id'])

        return _ok({
            'token': token,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'display_name': user.get('display_name', ''),
                'platform': 'wechat',
                'platform_user_id': unionid,
                'is_new_user': False,
            }
        })
    except Exception as e:
        import logging
        logging.error(f'[IMGateway] WeChat login failed: {e}')
        return _err(f'Login failed: {e}', 500)


def _telegram_login(data):
    """Handle Telegram Mini App login (initData HMAC verification).

    bot_token 来自 mini_app_builder 的 dev_accounts（与 routes_miniapp.py
    同一复用先例）；mini_app_builder 未启用时返回明确错误。
    """
    init_data = data.get('initData', '')
    if not init_data:
        return _err('initData is required', 400)

    try:
        # Verify HMAC signature (bot_token from mini_app_builder dev_accounts)
        try:
            from plugins.mini_app_builder.submodules.accounts.models import get_by_platform_raw
            from plugins.mini_app_builder.submodules.accounts.crypto import decrypt
        except ImportError:
            return _err('mini_app_builder plugin is required for Telegram login', 500)

        account = get_by_platform_raw('telegram')
        if not account or not account.get('bot_token'):
            return _err('Telegram bot not configured', 500)

        bot_token = decrypt(account['bot_token'])

        # Parse initData
        params = parse_qs(init_data)
        received_hash = params.pop('hash', [None])[0]
        if not received_hash:
            return _err('Missing hash in initData', 400)

        # Build data_check_string
        data_pairs = sorted((k, unquote(v[0])) for k, v in params.items())
        data_check_string = '\n'.join(f'{k}={v}' for k, v in data_pairs)

        # Compute secret key
        secret_key = hmac.new(
            b'WebAppData', bot_token.encode(), hashlib.sha256
        ).digest()
        computed_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if computed_hash != received_hash:
            return _err('Invalid initData signature', 403)

        # Extract user info
        user_json = params.get('user', ['{}'])[0]
        user_info = json.loads(unquote(user_json))
        tg_user_id = str(user_info.get('id', ''))
        tg_username = user_info.get('username', '')
        tg_first_name = user_info.get('first_name', '')

        display_name = tg_first_name or tg_username or f'TG{tg_user_id}'
        username = 'tg_' + hashlib.md5(tg_user_id.encode()).hexdigest()[:12]

        _ensure_auth_center()
        from services.user_registry import register_or_get_platform_user
        user = register_or_get_platform_user(
            'telegram', tg_user_id, username, display_name, '')

        from plugins.mini_app_builder.platform_users import upsert_mapping
        upsert_mapping('telegram', tg_user_id, user['id'], username, display_name, '')

        token = _issue_platform_jwt(user['id'])

        return _ok({
            'token': token,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'display_name': user.get('display_name', ''),
                'platform': 'telegram',
                'platform_user_id': tg_user_id,
                'is_new_user': False,
            }
        })
    except Exception as e:
        import logging
        logging.error(f'[IMGateway] Telegram login failed: {e}')
        return _err(f'Login failed: {e}', 500)


def _verify_line_token(access_token):
    """Server-side verify a LINE access token via LINE profile API.

    Returns (user_id, display_name, picture_url) when valid, else None.
    The client-supplied userId is never trusted directly.
    """
    import urllib.request
    req = urllib.request.Request(
        'https://api.line.me/v2/profile',
        headers={'Authorization': f'Bearer {access_token}'},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('userId'):
            return (
                data['userId'],
                data.get('displayName', ''),
                data.get('pictureUrl', ''),
            )
    except Exception:
        return None
    return None


def _line_login(data):
    """Handle LINE LIFF login — accessToken verified via LINE profile API."""
    access_token = data.get('accessToken', '')
    if not access_token:
        return _err('accessToken is required', 400)

    try:
        verified = _verify_line_token(access_token)
        if not verified:
            return _err('Invalid LINE access token', 403)
        user_id, platform_display_name, platform_avatar = verified

        _ensure_auth_center()

        username = 'line_' + hashlib.md5(user_id.encode()).hexdigest()[:12]
        display_name = (data.get('displayName') or data.get('nickname')
                        or platform_display_name or 'LINE User')
        avatar = data.get('avatar', '') or platform_avatar

        from services.user_registry import register_or_get_platform_user
        user = register_or_get_platform_user(
            'line', user_id, username, display_name, avatar)

        from plugins.mini_app_builder.platform_users import upsert_mapping
        upsert_mapping('line', user_id, user['id'], username, display_name, avatar)

        token = _issue_platform_jwt(user['id'])

        return _ok({
            'token': token,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'display_name': user.get('display_name', ''),
                'platform': 'line',
                'platform_user_id': user_id,
                'is_new_user': False,
            }
        })
    except Exception as e:
        import logging
        logging.error(f'[IMGateway] LINE login failed: {e}')
        return _err(f'Login failed: {e}', 500)
