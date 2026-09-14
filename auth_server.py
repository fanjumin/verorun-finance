#!/usr/bin/env python3
"""auth-center standalone server (port 8081) - login/OAuth/user/CMS/payment + Main Site"""
import sys, os

# Load .env BEFORE any auth imports (dotnet may be optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
if '' in sys.path:
    sys.path.remove('')
AUTH_DIR = os.path.join(_SCRIPT_DIR, 'auth-center')
sys.path.insert(0, AUTH_DIR)
sys.path.append(_SCRIPT_DIR)

from services.deployment_config import deploy

from flask import Flask, render_template, make_response, request, jsonify, redirect
import urllib.request as _ur
from auth_blueprint import register_auth


# ── Edition 服务门控（单一事实源：deploy/editions/<edition>.yaml 的 services: 段）──
try:
    from agent_matrix.models import is_service_enabled
except Exception as _e:
    print(f'[Edition] ⚠️ is_service_enabled import failed, default all-enabled: {_e}')

    def is_service_enabled(_name):
        return True

_MAIN_SITE_ENABLED = is_service_enabled('main_site')
_USER_LOGIN_ENABLED = is_service_enabled('user_login')


def _site_route(rule, **kw):
    """主站禁用（桌面包裹版）时只定义不注册路由（请求 404），官方版正常注册。"""
    if not _MAIN_SITE_ENABLED:
        return lambda fn: fn
    return app.route(rule, **kw)


def _is_edu():
    """教育版：无主站/无用户登录/无用户面板，所有公开入口统一导向 Admin 登录。"""
    return os.environ.get('DEPLOY_TYPE', '').strip().lower() == 'edu'


def _edu_admin_login():
    """教育版公开入口统一重定向到管理后台登录。"""
    return redirect('/admin/login')

app = Flask(__name__)

# Main site (www) templates live under site/templates and platform/templates, not beside this file.
# Mount them explicitly on the jinja loader to avoid TemplateNotFound for public_home.html etc.
# Also mount every plugin's templates/ dir so plugin routes (shop, subscription, ...) can render.
import jinja2
import glob as _glob

_plugin_tpl_dirs = sorted(
    d for d in _glob.glob(os.path.join(_SCRIPT_DIR, 'plugins', '*', 'templates'))
    if os.path.isdir(d)
)
app.jinja_loader = jinja2.ChoiceLoader([
    jinja2.FileSystemLoader(os.path.join(_SCRIPT_DIR, 'site', 'templates')),
    jinja2.FileSystemLoader(os.path.join(_SCRIPT_DIR, 'main_site', 'templates')),
] + [jinja2.FileSystemLoader(d) for d in _plugin_tpl_dirs] + [
    app.jinja_loader,
])

app.secret_key = os.environ.get('JWT_SECRET', 'dev-secret-key-change-in-production')
app.config['SESSION_TYPE'] = 'filesystem'

# ══ Try to load i18n ══
try:
    from i18n import _, get_lang, get_all_translations, resolve_locale
    _t = _
except Exception:
    _t = lambda s: s

# ── PluginManager ──
try:
    from plugin_manager.manager import PluginManager
    app.plugins_dir = os.path.join(_SCRIPT_DIR, 'plugins')
    PluginManager(app)
    print('[PluginManager] ✅ Auth service plugin manager initialized')
    # 技能注册中心（P0）：可用性求值 + 事件联动（开关关闭则回退旧行为）
    try:
        from plugin_manager.skill_registry import init_skill_registry
        init_skill_registry(app.extensions.get('plugin_manager'))
    except Exception as e:
        print(f'[SkillRegistry] ⚠️ auth service init skipped: {e}')
except Exception as e:
    print(f'[PluginManager] ⚠️ Auth service initialization failed: {e}')

# 桌面包裹版（user_login 禁用）：用户登录 / 用户资料蓝本不注册；
# admin / cms_admin / agent / session 保留（管理端仍需使用）。
_exclude_bps = []
if not _USER_LOGIN_ENABLED:
    _exclude_bps += ['auth', 'user']
register_auth(app, exclude_blueprints=_exclude_bps)

# ── Main site CMS public routes (/services, /cases, ...) ──
if _MAIN_SITE_ENABLED:
    try:
        from main_site.cms_public import cms_bp
        app.register_blueprint(cms_bp)
        print('[CMS Public] ✅ Main site public CMS routes registered')
    except Exception as e:
        print(f'[CMS Public] ⚠️ Main site public CMS routes failed: {e}')

# ── OAuth Plugin third-party login routes（user_login 禁用时不注册）──
if _USER_LOGIN_ENABLED:
    try:
        from plugins.oauth_config.routes.auth import oauth_bp
        app.register_blueprint(oauth_bp)
        print('[OAuth Plugin] ✅ Third-party login routes registered')
    except ImportError:
        print('[OAuth Plugin] ⚠️ OAuth plugin not installed, third-party login unavailable')
    except Exception as e:
        print(f'[OAuth Plugin] ⚠️ Load failed: {e}')

try:
    from flask_cors import CORS
    CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
except Exception:
    pass


def _get_site_plans():
    """站点套餐列表（真实数据源：插件 subscription/sub_items 表）。
    优先 site_% 建站套餐；若为空则回退全量 SKU，保证定价区有卡片。
    `subscription_plans` 旧表已随订阅解耦下线，禁止再查询它。"""
    plans = []
    try:
        from plugins.subscription.services import get_subscription_service
        svc = get_subscription_service()
        items = svc.list_site_plans() or []
        if not items:
            items = svc.list_items() or []
        for it in items:
            plans.append({
                'name': it.get('name', ''),
                'description': it.get('description', ''),
                'price_year': (it.get('price_year') or 0) // 100,   # 分 → 元
                'price_month': (it.get('price_month') or 0) // 100,
                'tier': it.get('tier', ''),
                'features': it.get('features', []) or [],
            })
    except Exception as e:
        print(f'[Site Plans] fallback empty: {e}')
    return plans


@app.route('/')
def site_home():
    """Render the main landing page using the existing theme template."""
    if _is_edu():
        return _edu_admin_login()
    site_plans = _get_site_plans()
    resp = make_response(render_template('public_home.html', LANG=deploy.LANG, site_plans=site_plans))
    # Set cross-subdomain SSO cookie if token present in URL
    token = request.args.get('token', '')
    if token and len(token) > 20:
        try:
            from services.jwt_service import validate_token
            if validate_token(token):
                main_domain = os.environ.get('DEPLOY_DOMAIN', '')
                _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
                if main_domain:
                    resp.set_cookie('sso_token', token, domain='.' + main_domain,
                                    path='/', max_age=604800, samesite='Lax',
                                    secure=_is_https, httponly=True)
        except Exception:
            pass
    return resp


@app.route('/pricing')
def site_pricing():
    """独立定价页：渲染 site_pricing.html，套餐卡片来自插件 sub_items（DB 驱动）。"""
    if _is_edu():
        return _edu_admin_login()
    import json as _json
    site_plans = _get_site_plans()
    brand = {}
    try:
        from services.brand_service import get_brand_settings
        brand = get_brand_settings() or {}
    except Exception:
        pass
    site = {
        'name': brand.get('site_name_en') or brand.get('site_name_cn') or 'VeroRun',
        'theme_color': brand.get('theme_color') or '#6366f1',
        'accent_color': brand.get('accent_color') or '#8b5cf6',
    }
    # 适配 site_pricing.html 的 plans 字段（price 元 / period / features 为 JSON 字符串）
    plans = []
    for p in site_plans:
        try:
            feats = _json.dumps(p.get('features', []), ensure_ascii=False)
        except Exception:
            feats = '[]'
        plans.append({
            'name': p.get('name', ''),
            'price': p.get('price_year', 0),
            'period': 'year',
            'features': feats,
            'tier': p.get('tier', ''),
        })
    return render_template('site_pricing.html', lang=deploy.LANG, site=site, plans=plans, tiers={})


@app.route('/features')
def site_features():
    if _is_edu():
        return _edu_admin_login()
    site_plans = _get_site_plans()
    return render_template('public_home.html', LANG=deploy.LANG, site_plans=site_plans)


@_site_route('/contact')
def site_contact():
    if _is_edu():
        return _edu_admin_login()
    site_plans = _get_site_plans()
    return render_template('public_home.html', LANG=deploy.LANG, site_plans=site_plans)


@app.route('/login')
def login_page():
    """Unified SSO login page."""
    if _is_edu():
        return _edu_admin_login()
    from services.brand_service import get_brand_settings
    brand = get_brand_settings() or {}
    from version import get_version
    return render_template('login.html', LANG=deploy.LANG, brand=brand, version=get_version())


@_site_route('/register')
def register_page():
    """Unified SSO register page."""
    if _is_edu():
        return _edu_admin_login()
    from services.brand_service import get_brand_settings
    brand = get_brand_settings() or {}
    from version import get_version
    return render_template('register.html', LANG=deploy.LANG, brand=brand, version=get_version())


# ══ Captcha proxy → admin:8084 ══
def _proxy_captcha(path, data=None, method='GET'):
    url = 'http://127.0.0.1:8084' + path
    req = _ur.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    resp = _ur.urlopen(req, timeout=5)
    return resp.read(), resp.status, {'Content-Type': resp.headers.get('Content-Type', 'application/json')}


@app.route('/api/captcha/generate', methods=['GET'])
def captcha_generate():
    try:
        return _proxy_captcha('/api/captcha/generate')
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/captcha/verify', methods=['POST'])
def captcha_verify():
    try:
        return _proxy_captcha('/api/captcha/verify', request.get_data())
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/captcha/consume', methods=['POST'])
def captcha_consume():
    try:
        return _proxy_captcha('/api/captcha/consume', request.get_data())
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.context_processor
def inject_globals():
    return dict(_=_t, lang=get_lang(), translations=get_all_translations(get_lang()),
                deploy=deploy, edition=os.environ.get('VR_EDITION', ''))


# ══ i18n 语言协商 + 语言切换（i18n-standard §5）══
@app.before_request
def _i18n_resolve_locale():
    """按 ?lang= → Cookie → Accept-Language → DEPLOY_LANG 协商请求语言。"""
    try:
        from flask import g
        g.lang_code = resolve_locale(
            lang_param=request.args.get('lang'),
            cookie=request.cookies.get('lang'),
            accept_header=request.headers.get('Accept-Language') or '',
        )
    except Exception:
        pass


import functools
from flask import render_template_string


@functools.lru_cache(maxsize=2)
def _lang_switch_widget(lang):
    return render_template_string('{% include "_lang_switch.html" %}', lang=lang)


@app.after_request
def inject_lang_switch(response):
    """对 text/html 响应自动注入语言切换按钮（_lang_switch.html）。

    跳过条件：非 text/html、无 </body>、页面含 data-lang-switch-off、
    URL 带 no_lang_switch=1。注入失败静默跳过，绝不影响页面本身。
    """
    try:
        if response.mimetype != 'text/html':
            return response
        data = response.get_data(as_text=True)
        if '</body>' not in data or 'data-lang-switch-off' in data:
            return response
        if request.args.get('no_lang_switch'):
            return response
        response.set_data(data.replace('</body>', _lang_switch_widget(get_lang()) + '</body>'))
    except Exception:
        pass
    return response


@app.route('/api/v1/i18n/lang', methods=['GET', 'POST'])
def i18n_set_lang():
    """查询 / 切换当前语言（写 Cookie，刷新后由 resolve_locale 生效）。"""
    from i18n import _normalize_locale, SUPPORTED_LOCALES
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        lang = _normalize_locale(data.get('lang') or request.args.get('lang'))
        if not lang or lang not in SUPPORTED_LOCALES:
            return jsonify({'ok': False, 'error': _t('Unsupported language')}), 400
        resp = jsonify({'ok': True, 'lang': lang})
        resp.set_cookie('lang', lang, max_age=365 * 24 * 3600, samesite='Lax')
        return resp
    return jsonify({'ok': True, 'lang': get_lang()})


@app.route('/health')
def health():
    """Liveness probe — used by health checks/watchdog to probe 8081 main site liveness."""
    return jsonify({'status': 'ok', 'service': 'auth-center+site'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))
    print(f'[Auth-Center+Site] starting on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
