#!/usr/bin/env python3
"""OAuth Plugin — 第三方登录路由（登录/回调/provider列表）

搬迁自 auth-center/routes/auth.py。
提供了一组独立的 Blueprint，由 AuthServer 注册。
"""
from i18n import _
import os, urllib.parse, time

from flask import Blueprint, request, jsonify, make_response, redirect as flask_redirect, url_for
from models import get_db, now_iso
from services.jwt_service import create_token, validate_token
from services.deployment_config import deploy

oauth_bp = Blueprint('oauth', __name__, url_prefix='/auth')

PROVIDER_NAMES = {
    'douyin': _('TikTok'), 'wechat': _('WeChat'), 'alipay': _('Alipay'),
    'google': 'Google', 'github': 'GitHub', 'facebook': 'Facebook',
    'telegram': 'Telegram',
}

# Whitelist for provider-specific id_field values (prevents SQL injection via f-string field names)
ID_FIELD_WHITELIST = {
    'douyin_open_id', 'wechat_open_id', 'alipay_open_id',
    'google_open_id', 'github_open_id', 'facebook_open_id',
    'telegram_open_id', 'alipay_user_id',
}

def _safe_id_field(id_field):
    """Validate id_field is in the whitelist (prevents SQL injection via f-string field names)."""
    if id_field not in ID_FIELD_WHITELIST:
        raise ValueError(f'Invalid id_field: {id_field} - must be one of {sorted(ID_FIELD_WHITELIST)}')
    return id_field

def _get_site_domain():
    host = request.headers.get('Host', '')
    domain = host.split(':')[0]
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain

def _get_cookie_domain():
    try:
        from services.brand_service import get_brand_settings
        brand = get_brand_settings()
        if brand and brand.get('site_domain', '').strip():
            return '.' + brand['site_domain'].strip().lower()
    except Exception:
        pass
    domain = _get_site_domain()
    if domain:
        return '.' + domain
    return None

def api_ok(data=None):
    return jsonify({'success': True, 'data': data})

def api_err(msg, code=400):
    return jsonify({'success': False, 'error': msg}), code


# =============================================
# OAuth provider list (dynamic frontend rendering)
# =============================================
@oauth_bp.route('/oauth/providers', methods=['GET'])
def oauth_providers():
    """Return enabled OAuth providers for the frontend login page (max 2)."""
    from plugins.oauth_config.services.oauth_service import get_enabled_oauth_providers
    providers = get_enabled_oauth_providers()
    return jsonify({'success': True, 'data': providers})


# =============================================
# Authlib-based OAuth — 统一第三方登录
# =============================================
@oauth_bp.route('/oauth/<provider>/login', methods=['GET'])
def oauth_login(provider):
    """Initiate OAuth login via authlib or provider-specific URL."""
    from plugins.oauth_config.services.oauth_service import oauth, get_douyin_oauth_url, is_intl_oauth_provider, get_intl_oauth_provider
    import secrets

    # ── International OAuth providers (Google/GitHub/Facebook) ──
    if is_intl_oauth_provider(provider):
        prov = get_intl_oauth_provider(provider)
        if not prov or not prov.is_configured():
            return flask_redirect(f'/login?error={provider} login not configured')
        redirect_uri = url_for('oauth.oauth_callback', provider=provider, _external=True, _scheme='https')
        auth_url = prov.get_authorize_url(redirect_uri)
        return flask_redirect(auth_url)

    if provider == 'alipay':
        from plugins.oauth_config.services.alipay_service import _get_config as ali_get_cfg
        cfg = ali_get_cfg(site_domain=deploy.url("platform").replace('https://', ''))
        if not cfg:
            return flask_redirect(f'/{provider}-login?error=Not configured')
        callback = f'{deploy.url("platform")}/auth/oauth/alipay/callback'
        params = urllib.parse.urlencode({
            'app_id': cfg['client_key'],
            'scope': 'auth_user',
            'redirect_uri': callback,
            'state': 'login',
        })
        url = f'https://openauth.alipay.com/oauth2/publicAppAuthorize.htm?{params}'
        return flask_redirect(url)

    if provider == 'douyin':
        site_domain = _get_site_domain()
        redirect_to = request.args.get('redirect', '')
        if not redirect_to:
            redirect_to = f'https://{site_domain}/'
        url = get_douyin_oauth_url(site_domain, redirect_to=redirect_to)
        if not url:
            return flask_redirect(f'/login?error=Douyin login not configured')
        return flask_redirect(url)

    client = getattr(oauth, provider, None)
    if not client:
        return flask_redirect(f'/login?error=Unsupported login method')
    redirect_uri = url_for('oauth.oauth_callback', provider=provider, _external=True, _scheme='https')
    return client.authorize_redirect(redirect_uri)


@oauth_bp.route('/oauth/<provider>/callback', methods=['GET'])
def oauth_callback(provider):
    """OAuth callback — handle code exchange, user lookup, JWT creation."""
    from plugins.oauth_config.services.oauth_service import oauth, get_douyin_userinfo, is_intl_oauth_provider, get_intl_oauth_provider

    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    domain = _get_site_domain()

    # ── Telegram OAuth (special: hash verification) ──
    if provider == 'telegram':
        prov = get_intl_oauth_provider(provider)
        if not prov:
            return flask_redirect(f'https://{domain}/login?error=telegram not configured')
        raw_query = request.query_string.decode('utf-8')
        user_info = prov.get_user_by_code(raw_query, '')
        if 'error' in user_info:
            return flask_redirect(f'https://{domain}/login?error={urllib.parse.quote(user_info["error"][:50])}')
        open_id = user_info.get('open_id', '')
        nickname = user_info.get('nickname', '')
        avatar = user_info.get('avatar', '')
        id_field = _safe_id_field('telegram_open_id')
        display_name = nickname or f'Telegram user {open_id[-4:]}'
        now = now_iso()
        with get_db() as conn:
            cur = conn.execute(f'SELECT * FROM public.users WHERE {id_field}=%s', (open_id,))
            user_row = cur.fetchone()
            if user_row:
                user = dict(user_row)
                # VR-AUTH-007：冻结账号（active=0）禁止登录
                if user.get('active', 1) != 1:
                    return flask_redirect(f'https://{domain}/login?error=' + urllib.parse.quote(_('Account is disabled')))
                conn.execute('UPDATE public.users SET last_login=%s, display_name=%s WHERE id=%s',
                             (now, display_name or user.get('display_name', ''), user['id']))
            else:
                cur = conn.execute(
                    f'INSERT INTO public.users ({id_field}, display_name, avatar_url, last_login) VALUES (%s,%s,%s,%s) RETURNING id',
                    (open_id, display_name, avatar, now))
                user_id = cur.fetchone()['id']
                conn.execute(
                    'INSERT INTO public.app_authorizations (user_id, app_name, tier) VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING',
                    (user_id, 'trademind', 'free'))
                user = {'id': user_id, id_field: open_id, 'display_name': display_name}
            conn.commit()
        jwt = create_token(user['id'], app_name='trademind')
        callback_url = f'https://{main_domain}/?token={jwt}'
        cd_val = '.' + main_domain
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(flask_redirect(callback_url))
        resp.set_cookie('sso_token', jwt, domain=cd_val, path='/', max_age=604800,
                        httponly=True, secure=_is_https, samesite='Lax')
        return resp

    # ── International OAuth providers (Google/GitHub/Facebook) ──
    if is_intl_oauth_provider(provider):
        prov = get_intl_oauth_provider(provider)
        if not prov:
            return flask_redirect(f'https://{domain}/login?error={provider} not configured')
        code = request.args.get('code', '')
        if not code:
            return flask_redirect(f'https://{domain}/login?error=Missing authorization code')
        redirect_uri = url_for('oauth.oauth_callback', provider=provider, _external=True, _scheme='https')
        user_info = prov.get_user_by_code(code, redirect_uri)
        if 'error' in user_info:
            return flask_redirect(f'https://{domain}/login?error={urllib.parse.quote(user_info["error"][:50])}')
        open_id = user_info.get('open_id', '')
        nickname = user_info.get('nickname', '')
        avatar = user_info.get('avatar', '')
        email = user_info.get('email', '')
        id_field = _safe_id_field(f'{provider}_open_id')
        display_name = nickname or email or f'{provider} user {open_id[-4:]}'
        now = now_iso()
        with get_db() as conn:
            cur = conn.execute(f'SELECT * FROM public.users WHERE {id_field}=%s', (open_id,))
            user_row = cur.fetchone()
            if user_row:
                user = dict(user_row)
                # VR-AUTH-007：冻结账号（active=0）禁止登录
                if user.get('active', 1) != 1:
                    return flask_redirect(f'https://{domain}/login?error=' + urllib.parse.quote(_('Account is disabled')))
                conn.execute('UPDATE public.users SET last_login=%s, display_name=%s WHERE id=%s',
                             (now, display_name or user.get('display_name', ''), user['id']))
            else:
                cur = conn.execute(
                    f'INSERT INTO public.users ({id_field}, display_name, email, avatar_url, last_login) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                    (open_id, display_name, email, avatar, now))
                user_id = cur.fetchone()['id']
                conn.execute(
                    'INSERT INTO public.app_authorizations (user_id, app_name, tier) VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING',
                    (user_id, 'trademind', 'free'))
                user = {'id': user_id, id_field: open_id, 'display_name': display_name}
            conn.commit()
        jwt = create_token(user['id'], app_name='trademind')
        callback_url = f'https://{main_domain}/?token={jwt}'
        cd_val = '.' + main_domain
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(flask_redirect(callback_url))
        resp.set_cookie('sso_token', jwt, domain=cd_val, path='/', max_age=604800,
                        httponly=True, secure=_is_https, samesite='Lax')
        return resp

    # ── Alipay OAuth ──
    if provider == 'alipay':
        from plugins.oauth_config.services.alipay_service import get_access_token as ali_get_token, get_user_info as ali_get_user
        auth_code = request.args.get('auth_code', '')
        if not auth_code:
            return flask_redirect(f'https://{domain}/login?error=Missing authorization code')
        token_data = ali_get_token(auth_code, site_domain=domain)
        if 'error' in token_data:
            err_msg = token_data['error']
            if err_msg == 'stub mode':
                alipay_user_id = token_data.get('stub_user_id', 'stub_alipay')
                nickname = _('Alipay User')
                avatar = ''
                id_field = 'alipay_user_id'
                open_id = alipay_user_id
            else:
                return flask_redirect(f'https://{domain}/login?error=Alipay API: {urllib.parse.quote(err_msg[:50])}')
        else:
            alipay_user_id = token_data['user_id']
            access_token = token_data['access_token']
            info = ali_get_user(access_token, site_domain=domain)
            nickname = info.get('nickname', '') if 'error' not in info else ''
            avatar = info.get('avatar', '') if 'error' not in info else ''
            id_field = 'alipay_user_id'
            open_id = alipay_user_id

    # ── Douyin OAuth ──
    if provider == 'douyin':
        from plugins.oauth_config.services.douyin_service import get_access_token as dy_get_token, get_user_info as dy_get_user
        code = request.args.get('code', '')
        if not code:
            return flask_redirect(f'https://{domain}/login?error=Missing authorization code')
        token_data = dy_get_token(code, site_domain=domain)
        if 'error' in token_data:
            err_msg = token_data['error']
            return flask_redirect(f'https://{domain}/login?error=Login failed')
        open_id = token_data['open_id']
        access_token = token_data['access_token']
        info = dy_get_user(open_id, access_token, site_domain=domain)
        nickname = info.get('nickname', '') if 'error' not in info else ''
        avatar = info.get('avatar', '') if 'error' not in info else ''
        id_field = 'douyin_open_id'

    # ── WeChat callback via state parsing ──
    state = request.args.get('state', '')
    redirect_to = f'https://{domain}/'
    if '|' in state:
        _, encoded_redirect = state.split('|', 1)
        redirect_to = urllib.parse.unquote(urllib.parse.unquote(encoded_redirect))
        if not redirect_to.startswith('http'):
            redirect_to = f'https://{domain}/'

    if provider not in ('alipay', 'douyin'):
        # WeChat or other CN provider via authlib
        from plugins.oauth_config.services.oauth_service import oauth
        client = getattr(oauth, provider, None)
        if not client:
            return flask_redirect(f'https://{domain}/login?error=Unsupported provider')
        try:
            token = client.authorize_access_token()
        except Exception as e:
            return flask_redirect(f'https://{domain}/login?error=Token exchange failed')
        open_id = token.get('openid', '')
        nickname = token.get('nickname', '')
        avatar = token.get('avatar', '')
        id_field = _safe_id_field(f'{provider}_open_id')

    # Find or create user
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute(f'SELECT * FROM public.users WHERE {id_field}=%s', (open_id,))
        user_row = cur.fetchone()
        if user_row:
            user = dict(user_row)
            # VR-AUTH-007：冻结账号（active=0）禁止登录
            if user.get('active', 1) != 1:
                return flask_redirect(f'https://{domain}/login?error=' + urllib.parse.quote(_('Account is disabled')))
            conn.execute('UPDATE public.users SET last_login=%s, display_name=%s WHERE id=%s',
                         (now, nickname or user.get('display_name', ''), user['id']))
        else:
            display_name = nickname or f'{provider} User_{open_id[-4:]}'
            cur = conn.execute(
                f'INSERT INTO public.users ({id_field}, display_name, last_login) VALUES (%s,%s,%s) RETURNING id',
                (open_id, display_name, now))
            user_id = cur.fetchone()['id']
            conn.execute(
                'INSERT INTO public.app_authorizations (user_id, app_name, tier) VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING',
                (user_id, 'trademind', 'free'))
            user = {'id': user_id, id_field: open_id, 'display_name': display_name}
        conn.commit()

    jwt = create_token(user['id'], app_name='trademind')
    parsed = urllib.parse.urlparse(redirect_to)
    final_path = parsed.path or '/'
    if parsed.query:
        final_path += '?' + parsed.query
    callback_url = f'https://{main_domain}/?token={jwt}&redirect={urllib.parse.quote(final_path, safe="")}'
    cd_val = '.' + main_domain
    _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
    resp = make_response(flask_redirect(callback_url))
    resp.set_cookie('sso_token', jwt, domain=cd_val, path='/', max_age=604800,
                    httponly=True, secure=_is_https, samesite='Lax')
    return resp


# =============================================
# WeChat QR login + callback (legacy paths)
# =============================================
@oauth_bp.route('/wechat/qr', methods=['GET'])
def wechat_qr():
    from plugins.oauth_config.services.wechat_service import is_stub, get_qr_url
    from flask import render_template
    if not is_stub():
        qr_url = get_qr_url()
        if qr_url:
            return flask_redirect(qr_url)
    return render_template('douyin_login.html')


@oauth_bp.route('/wechat/callback', methods=['GET'])
def wechat_callback():
    from plugins.oauth_config.services.wechat_service import get_openid_by_code, get_user_info, is_stub
    domain = _get_site_domain()
    code = request.args.get('code', '')
    state = request.args.get('state', 'login')
    if not code:
        return flask_redirect(f'https://{domain}/wechat-login?error=Missing authorization code')
    wx = get_openid_by_code(code)
    if 'error' in wx:
        return flask_redirect(f'https://{domain}/wechat-login?error=' + wx['error'])
    openid = wx['openid']
    access_token = wx.get('access_token', '')
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute('SELECT * FROM public.users WHERE wechat_openid=%s', (openid,))
        user = cur.fetchone()
        if user:
            user = dict(user)
            # VR-AUTH-007：冻结账号（active=0）禁止登录
            if user.get('active', 1) != 1:
                return flask_redirect(f'https://{domain}/wechat-login?error=' + urllib.parse.quote(_('Account is disabled')))
            conn.execute('UPDATE public.users SET last_login=%s WHERE id=%s', (now, user['id']))
            if access_token and user.get('wechat_unionid'):
                try:
                    info = get_user_info(openid, access_token)
                    if 'nickname' in info and info['nickname']:
                        conn.execute('UPDATE public.users SET wechat_nickname=%s, avatar_url=%s WHERE id=%s',
                                     (info['nickname'], info.get('avatar', ''), user['id']))
                except:
                    pass
        else:
            nickname = ''
            avatar = ''
            unionid = wx.get('unionid', '')
            try:
                info = get_user_info(openid, access_token)
                if 'nickname' in info:
                    nickname = info.get('nickname', '')
                    avatar = info.get('avatar', '')
                    unionid = info.get('unionid', unionid)
            except:
                pass
            cur = conn.execute(
                'INSERT INTO public.users (wechat_openid, wechat_unionid, wechat_nickname, avatar_url, last_login) '
                'VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (openid, unionid, nickname, avatar, now))
            user_id = cur.fetchone()['id']
            conn.execute(
                'INSERT INTO public.app_authorizations (user_id, app_name, tier) VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING',
                (user_id, 'trademind', 'free'))
            user = {'id': user_id, 'wechat_openid': openid, 'wechat_nickname': nickname}
        conn.commit()
    token = create_token(user['id'], app_name='trademind')
    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    return flask_redirect(f'https://{main_domain}/?token={token}')


# =============================================
# Douyin QR login
# =============================================
@oauth_bp.route('/douyin/qr', methods=['GET'])
def douyin_qr():
    from plugins.oauth_config.services.douyin_service import is_stub as dy_is_stub, get_oauth_url as dy_get_url
    from flask import render_template
    domain = _get_site_domain()
    redirect_to = request.args.get('redirect', '') or request.referrer or '/'
    state = f'login:{redirect_to}'
    if not dy_is_stub(domain):
        oauth_url = dy_get_url(site_domain=domain, state=state)
        if oauth_url:
            return flask_redirect(oauth_url)
    return render_template('douyin_login.html', redirect_to=redirect_to)


# =============================================
# Douyin callback
# =============================================
@oauth_bp.route('/douyin/callback', methods=['GET'])
def douyin_callback():
    from plugins.oauth_config.services.douyin_service import get_access_token as dy_get_token, get_user_info as dy_get_user, is_stub as dy_is_stub
    domain = _get_site_domain()
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    redirect_to = '/'
    if ':' in state:
        parts = state.split(':', 1)
        if len(parts) == 2 and parts[0] == 'login':
            redirect_to = parts[1]
    if redirect_to in ('/login', '/register', '/reset-password', '/douyin-login', '/wechat-login'):
        redirect_to = '/'
    if not code:
        return flask_redirect(f'https://{domain}/douyin-login?error=Missing authorization code')
    
    if dy_is_stub(domain) and code.startswith('stub_'):
        open_id = 'stub_open_' + code[5:13]
        nickname = _('TikTok User_') + open_id[-4:]
        avatar = ''
    else:
        token_data = dy_get_token(code, site_domain=domain)
        if 'error' in token_data:
            return flask_redirect(f'https://{domain}/douyin-login?error={token_data["error"]}')
        open_id = token_data['open_id']
        info = dy_get_user(open_id, token_data['access_token'], site_domain=domain)
        nickname = info.get('nickname', '') if 'error' not in info else ''
        avatar = info.get('avatar', '') if 'error' not in info else ''
    
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute('SELECT * FROM public.users WHERE douyin_open_id=%s', (open_id,))
        user = cur.fetchone()
        if user:
            user = dict(user)
            # VR-AUTH-007：冻结账号（active=0）禁止登录
            if user.get('active', 1) != 1:
                return flask_redirect(f'https://{domain}/douyin-login?error=' + urllib.parse.quote(_('Account is disabled')))
            conn.execute('UPDATE public.users SET last_login=%s, douyin_nickname=%s, douyin_avatar=%s WHERE id=%s',
                         (now, nickname, avatar, user['id']))
        else:
            cur = conn.execute(
                'INSERT INTO public.users (douyin_open_id, douyin_nickname, douyin_avatar, display_name, last_login) '
                'VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (open_id, nickname, avatar, nickname or '', now))
            user_id = cur.fetchone()['id']
            conn.execute(
            'INSERT INTO public.app_authorizations (user_id, app_name, tier) VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING',
                (user_id, 'trademind', 'free'))
            user = {'id': user_id, 'douyin_open_id': open_id, 'douyin_nickname': nickname}
        conn.commit()
    
    jwt = create_token(user['id'], app_name='trademind')
    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
    cd_val = '.' + main_domain
    callback_url = f'https://{main_domain}/?token={jwt}'
    resp = make_response(flask_redirect(callback_url))
    resp.set_cookie('sso_token', jwt, domain=cd_val, path='/', max_age=604800,
                    httponly=True, secure=_is_https, samesite='Lax')
    return resp


# =============================================
# WeChat login (POST — mini-program / app)
# =============================================
@oauth_bp.route('/wechat/login', methods=['POST'])
def wechat_login():
    from plugins.oauth_config.services.wechat_service import get_openid_by_code
    data = request.get_json() or {}
    code = data.get('code', '').strip()
    if not code:
        return api_err('Missing WeChat authorization code')
    wx = get_openid_by_code(code)
    if 'error' in wx:
        return api_err('WeChat login failed: ' + wx['error'])
    openid = wx['openid']
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute('SELECT * FROM public.users WHERE wechat_openid=%s', (openid,))
        user = cur.fetchone()
        if user:
            user = dict(user)
            # VR-AUTH-007：冻结账号（active=0）禁止登录
            if user.get('active', 1) != 1:
                return api_err(_('Account is disabled'), 403)
            conn.execute('UPDATE public.users SET last_login=%s WHERE id=%s', (now, user['id']))
        else:
            cur = conn.execute(
                'INSERT INTO public.users (wechat_openid, wechat_unionid, last_login) VALUES (%s,%s,%s) RETURNING id',
                (openid, wx.get('unionid', ''), now))
            user_id = cur.fetchone()['id']
            conn.execute(
                'INSERT INTO public.app_authorizations (user_id, app_name, tier) VALUES (%s,%s,%s) ON CONFLICT (user_id, app_name) DO NOTHING',
                    (user_id, 'trademind', 'free'))
            user = {'id': user_id, 'wechat_openid': openid}
        conn.commit()
    token = create_token(user['id'], app_name='trademind')
    return api_ok({'token': token, 'user': {'id': user['id']}})
