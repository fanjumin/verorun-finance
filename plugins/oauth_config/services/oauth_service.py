#!/usr/bin/env python3
"""OAuth Service — authlib-based third-party login for Douyin, WeChat, Alipay.

Usage in each Flask app:
    from services.oauth_service import init_oauth
    init_oauth(app)
Then in routes:
    from services.oauth_service import oauth
    oauth.douyin.authorize_redirect(...)

NOTE: Douyin uses per-request dynamic credential lookup (multi-tenant).
      Global oauth.register() is only used for WeChat (single-tenant via env).
"""
from i18n import _
import os, json, urllib.request, urllib.parse
from authlib.integrations.flask_client import OAuth

oauth = OAuth()  # shared OAuth object, init with app later

# Market-based OAuth provider list (lazy-imported to avoid circular deps)
_INTL_OAUTH_PROVIDERS = []  # list of BaseOAuthProvider classes


def get_intl_oauth_providers():
    """Return list of registered international OAuth provider classes.
    Loaded lazily from providers.oauth package."""
    if not _INTL_OAUTH_PROVIDERS:
        try:
            from plugins.oauth_config.providers.google import GoogleOAuthProvider
            from plugins.oauth_config.providers.github import GitHubOAuthProvider
            from plugins.oauth_config.providers.facebook import FacebookOAuthProvider
            from plugins.oauth_config.providers.telegram import TelegramOAuthProvider
            from plugins.oauth_config.providers import register_provider
            for cls in (GoogleOAuthProvider, GitHubOAuthProvider, FacebookOAuthProvider, TelegramOAuthProvider):
                register_provider('intl', 'oauth', cls)
                _INTL_OAUTH_PROVIDERS.append(cls)
        except ImportError:
            pass  # providers package not available
    return list(_INTL_OAUTH_PROVIDERS)


def get_douyin_oauth_url(site_domain, callback_domain=None, redirect_to=''):
    """动态按域名获取抖音 OAuth 凭据并生成 URL。
    每次请求独立查询 DB，无全局竞态条件风险。"""
    try:
        from plugins.oauth_config.services.douyin_service import _get_config
        cfg = _get_config(site_domain)
        if not cfg:
            return None
        cb_domain = callback_domain or site_domain
        callback = f'https://{cb_domain}/auth/oauth/douyin/callback'
        import secrets, urllib.parse
        random_token = secrets.token_urlsafe(16)
        state = random_token
        if redirect_to:
            state = f'{random_token}|{urllib.parse.quote(redirect_to, safe="")}'
        params = urllib.parse.urlencode({
            'client_key': cfg['client_key'],
            'response_type': 'code',
            'scope': 'user_info',
            'redirect_uri': callback,
            'state': state,
        })
        return f'https://open.douyin.com/platform/oauth/connect?{params}'
    except Exception:
        return None

def init_oauth(app):
    """Register OAuth providers on a Flask app using authlib.
    WeChat uses global env-based credentials (single-tenant).
    Douyin uses per-request dynamic lookup (see get_douyin_oauth_url)."""
    oauth.init_app(app)

    # ── WeChat OAuth 2.0 (single-tenant, env-based) ──
    wx_id = os.environ.get('WECHAT_APP_ID', '')
    wx_secret = os.environ.get('WECHAT_APP_SECRET', '')
    if wx_id and wx_secret and wx_id != 'stub':
        oauth.register(
            name='wechat',
            client_id=wx_id,
            client_secret=wx_secret,
            authorize_url='https://open.weixin.qq.com/connect/qrconnect',
            access_token_url='https://api.weixin.qq.com/sns/oauth2/access_token',
            userinfo_url='https://api.weixin.qq.com/sns/userinfo',
            authorize_params={'scope': 'snsapi_login'},
            client_kwargs={'scope': 'snsapi_login'},
        )

    return oauth


def is_intl_oauth_provider(provider_name):
    """Check if a provider name is an international OAuth provider (Google/GitHub/Facebook)."""
    intl_providers = get_intl_oauth_providers()
    return any(cls.PROVIDER == provider_name for cls in intl_providers)


def get_intl_oauth_provider(provider_name):
    """Get an intl OAuth provider instance by name (e.g. 'google', 'github', 'telegram').
    Searches the already-loaded _INTL_OAUTH_PROVIDERS list by PROVIDER attribute,
    avoiding get_provider() which expects an int position parameter."""
    providers = get_intl_oauth_providers()
    for cls in providers:
        if hasattr(cls, 'PROVIDER') and cls.PROVIDER == provider_name:
            return cls()
    return None


# ── Enabled OAuth providers (for dynamic frontend rendering) ──
PROVIDER_NAMES = {
    'douyin': _('TikTok'), 'wechat': _('WeChat'), 'alipay': _('Alipay'),
    'google': 'Google', 'github': 'GitHub', 'facebook': 'Facebook',
    'telegram': 'Telegram',
}

def get_enabled_oauth_providers(max_providers=2):
    """Query enabled OAuth providers (DB + env) and return up to max_providers.

    Returns list of dicts: [{'provider': 'douyin', 'name': _('TikTok'), 'login_url': '/auth/oauth/douyin/login'}, ...]
    """
    enabled = []
    seen = set()

    # 1. DB-configured providers (oauth_providers 表 — 主库 public schema，§12.10)
    try:
        from plugins.oauth_config.models import get_db
        with get_db() as conn:
            rows = conn.execute(
                'SELECT DISTINCT provider FROM oauth_providers WHERE is_active=1 ORDER BY provider'
            ).fetchall()
        for row in rows:
            p = row['provider']
            if p not in seen:
                seen.add(p)
                enabled.append(p)
    except Exception:
        pass

    # 2. Env-based intl providers (google/github/facebook)
    for module_path, cls_name, provider_name in [
        ('plugins.oauth_config.providers.google', 'GoogleOAuthProvider', 'google'),
        ('plugins.oauth_config.providers.github', 'GitHubOAuthProvider', 'github'),
        ('plugins.oauth_config.providers.facebook', 'FacebookOAuthProvider', 'facebook'),
    ]:
        if provider_name in seen:
            continue
        try:
            mod = __import__(module_path, fromlist=[cls_name])
            cls = getattr(mod, cls_name)
            if cls().is_configured():
                seen.add(provider_name)
                enabled.append(provider_name)
        except Exception:
            pass

    # 3. Limit to max_providers
    enabled = enabled[:max_providers]

    return [
        {
            'provider': p,
            'name': PROVIDER_NAMES.get(p, p),
            'login_url': f'/auth/oauth/{p}/login',
        }
        for p in enabled
    ]


def get_douyin_userinfo(access_token, open_id):
    """Fetch Douyin user info (authlib handles token exchange but not Douyin's userinfo)."""
    url = ('https://open.douyin.com/oauth/userinfo?'
           + urllib.parse.urlencode({'access_token': access_token, 'open_id': open_id}))
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        body = json.loads(resp.read())
        d = body.get('data', {})
        return {
            'nickname': d.get('nickname', ''),
            'avatar': d.get('avatar', ''),
            'open_id': open_id,
        }
    except Exception as e:
        return {'error': str(e)}
