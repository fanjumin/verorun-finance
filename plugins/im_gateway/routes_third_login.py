#!/usr/bin/env python3
"""第三方登录 — Web OAuth 登录闭环路由（Phase 5，方案 B：插件自包含）。

登录入口 = IM Gateway 插件，登录内核 = auth-center（session_service 统一签发）。
提供：
- GET /api/v1/oauth/<provider>/login   发起授权（state 落库防 CSRF）→ 重定向平台
- GET /api/v1/oauth/<provider>/callback 平台回调 → code 换 token → 绑定 → JWT → sso_token cookie → 跳主站

用户绑定统一走 im_gateway.login_user_bindings 表（联邦身份，不扩展主库 users 结构）；
主库 users 按 username 唯一匹配 get-or-create（username = <provider>_<md5(openid)前12位>）。
"""
import hashlib
import logging
import os
import sys
import uuid
import urllib.parse
from datetime import datetime, timedelta

from flask import Blueprint, request
from flask import redirect as flask_redirect

from .login import get_login_provider_class
from .login.exchange import exchange_and_get_userinfo, OAuthExchangeError
from .models import get_im_db

logger = logging.getLogger(__name__)

# Blueprint（公开，无需管理员鉴权 —— 用户浏览器跳转）
third_login_bp = Blueprint('im_gateway_third_login', __name__,
                           url_prefix='/api/v1/oauth')

_STATE_TTL_MINUTES = 10


def _ensure_auth_center():
    """Ensure auth-center is importable (main_site removes it from sys.path
    after startup; services/models are not cached in sys.modules)."""
    _auth_center = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
    if os.path.isdir(_auth_center) and _auth_center not in sys.path:
        sys.path.insert(0, _auth_center)


def _callback_uri(provider: str) -> str:
    """回调地址（login 与 callback 必须严格一致，故统一自动计算）。"""
    return request.url_root.rstrip('/') + f'/api/v1/oauth/{provider}/callback'


def _read_config(provider: str) -> dict:
    """读取该平台第三方登录凭据。"""
    with get_im_db() as conn:
        row = conn.execute(
            'SELECT client_id, client_secret, scopes, redirect_uri, is_enabled '
            'FROM login_providers WHERE provider=%s', (provider,)
        ).fetchone()
    return dict(row) if row else {}


def _save_state(state: str, provider: str):
    """state 落库（10 分钟有效，一次性）。"""
    with get_im_db() as conn:
        conn.execute(
            "INSERT INTO oauth_login_states (state, provider, consumed, expires_at) "
            "VALUES (%s, %s, 0, %s)",
            (state, provider, datetime.now() + timedelta(minutes=_STATE_TTL_MINUTES)))
        conn.commit()


def _consume_state(state: str, provider: str) -> bool:
    """校验并消费 state（防 CSRF：一次性 + 过期作废）。"""
    with get_im_db() as conn:
        row = conn.execute(
            'SELECT id FROM oauth_login_states '
            'WHERE state=%s AND provider=%s AND consumed=0 AND expires_at > NOW()',
            (state, provider)).fetchone()
        if not row:
            return False
        conn.execute('UPDATE oauth_login_states SET consumed=1 WHERE id=%s', (row['id'],))
        conn.commit()
        return True


def _redirect_error(message: str):
    """失败统一跳主站 /login?error=...（消息截断防注入超长）。"""
    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    return flask_redirect('https://{domain}/login?error={msg}'.format(
        domain=main_domain, msg=urllib.parse.quote(str(message)[:200])))


def _get_or_create_user(provider: str, openid: str, user_info: dict) -> int:
    """主库 users get-or-create（username 唯一匹配）+ im_gateway 绑定表 upsert。

    与 mini_login 的 line 平台模式一致：不新增 users 列，绑定关系存独立 schema。
    """
    _ensure_auth_center()
    from models import get_db
    username = f'{provider}_' + hashlib.md5(openid.encode()).hexdigest()[:12]
    display_name = ((user_info or {}).get('nickname') or '')[:60] or f'{provider} user'
    avatar = ((user_info or {}).get('avatar') or '')[:500]
    with get_db() as conn:
        row = conn.execute(
            'SELECT id FROM public.users WHERE username=%s', (username,)).fetchone()
        if row:
            user_id = row['id']
            if display_name or avatar:
                conn.execute(
                    "UPDATE public.users SET last_login=NOW(), "
                    "avatar_url=COALESCE(NULLIF(%s,''), avatar_url), "
                    "display_name=COALESCE(NULLIF(%s,''), display_name) "
                    "WHERE id=%s", (avatar, display_name, user_id))
            conn.commit()
        else:
            try:
                cur = conn.execute(
                    "INSERT INTO public.users (username, display_name, avatar_url, "
                    "created_at, last_login) VALUES (%s,%s,%s,NOW(),NOW()) RETURNING id",
                    (username, display_name, avatar))
                user_id = cur.fetchone()['id']
                # 新用户补默认主站授权（同 oauth_config callback 先例，幂等）
                conn.execute(
                    "INSERT INTO public.app_authorizations (user_id, app_name, tier) "
                    "VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING",
                    (user_id, 'main', 'free'))
                conn.commit()
            except Exception:
                # username 唯一冲突：并发下另一请求已创建，回查复用
                conn.rollback()
                row = conn.execute(
                    'SELECT id FROM public.users WHERE username=%s', (username,)).fetchone()
                user_id = row['id']
        conn.close()
    # 绑定表 upsert（幂等）
    with get_im_db() as conn:
        exists = conn.execute(
            'SELECT id FROM login_user_bindings '
            'WHERE provider=%s AND provider_user_id=%s', (provider, openid)).fetchone()
        if exists:
            conn.execute(
                "UPDATE login_user_bindings SET user_id=%s, username=%s, display_name=%s, "
                "avatar=%s, updated_at=NOW() WHERE id=%s",
                (user_id, username, display_name, avatar, exists['id']))
        else:
            conn.execute(
                "INSERT INTO login_user_bindings (provider, provider_user_id, user_id, "
                "username, display_name, avatar) VALUES (%s,%s,%s,%s,%s,%s)",
                (provider, openid, user_id, username, display_name, avatar))
        conn.commit()
    return user_id


@third_login_bp.route('/<provider>/login', methods=['GET'])
def login(provider):
    """发起授权：校验提供方/凭据 → state 落库 → 重定向平台授权页。"""
    cls = get_login_provider_class(provider)
    if cls is None:
        return _redirect_error('Unsupported provider')
    cfg = _read_config(provider)
    if not cfg.get('client_id'):
        return _redirect_error('Provider not configured')
    if not int(cfg.get('is_enabled', 0) or 0):
        return _redirect_error('Provider disabled')
    state = uuid.uuid4().hex[:24]
    _save_state(state, provider)
    url = cls.build_authorize_url(
        client_id=cfg['client_id'],
        redirect_uri=_callback_uri(provider),
        state=state,
        scopes=cfg.get('scopes', ''))
    return flask_redirect(url)


@third_login_bp.route('/<provider>/callback', methods=['GET'])
def callback(provider):
    """平台回调：校验 state → code 换 token → 绑定 → auth-center 签发 → 跳主站。"""
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    if not code or not state:
        return _redirect_error('Missing code or state')
    if not _consume_state(state, provider):
        return _redirect_error('Invalid or expired state')
    cls = get_login_provider_class(provider)
    if cls is None:
        return _redirect_error('Unsupported provider')
    cfg = _read_config(provider)
    if not cfg.get('client_id'):
        return _redirect_error('Provider not configured')

    try:
        user_info = exchange_and_get_userinfo(
            provider, code, _callback_uri(provider),
            {'client_id': cfg.get('client_id'),
             'client_secret': cfg.get('client_secret', '')})
    except OAuthExchangeError as e:
        return _redirect_error(str(e))
    except Exception:
        logger.exception('[ThirdLogin] exchange failed: %s', provider)
        return _redirect_error('OAuth exchange failed')

    openid = (user_info or {}).get('openid', '')
    if not openid:
        return _redirect_error('Openid missing')

    try:
        user_id = _get_or_create_user(provider, openid, user_info)
    except Exception:
        logger.exception('[ThirdLogin] user binding failed: %s', provider)
        return _redirect_error('User binding failed')

    # 登录内核：auth-center 统一签发（含 2FA filter，2FA 拦截时抛 TwoFactorRequired）
    _ensure_auth_center()
    try:
        from services.session_service import issue_auth_session, TwoFactorRequired
        result = issue_auth_session(user_id, '', app_name='main')
    except TwoFactorRequired as e:
        main_domain = os.environ.get('DEPLOY_DOMAIN', '')
        challenge = urllib.parse.quote(str(e.challenge_token or ''))
        redirect_path = e.redirect or '/login'
        return flask_redirect(
            f'https://{main_domain}{redirect_path}?needs_2fa=1&challenge_token={challenge}')
    except Exception:
        logger.exception('[ThirdLogin] session issue failed: %s', provider)
        return _redirect_error('Session issue failed')

    jwt = result.get('token', '')
    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    resp = flask_redirect(f'https://{main_domain}/?token={jwt}')
    try:
        from services.session_service import set_sso_cookie
        set_sso_cookie(resp, jwt, app_name='main')
    except Exception:
        logger.exception('[ThirdLogin] set_sso_cookie failed')
    return resp
