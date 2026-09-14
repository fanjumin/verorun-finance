#!/usr/bin/env python3
# VeroRun 维洛智能 (verorun.com / verorun.cn)
# 版权所有 (c) 2026 樊聚民 (fanjumin). All Rights Reserved.

"""Platform — User Console (端口 8083)"""

import sys, os, sysconfig

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'auth-center'))

# Prepend stdlib path LAST so it stays at sys.path[0] and prevents
# the project's platform/ dir from shadowing stdlib platform module.
stdlib_dir = sysconfig.get_path('stdlib')
if stdlib_dir in sys.path:
    sys.path.remove(stdlib_dir)
sys.path.insert(0, stdlib_dir)

from dotenv import load_dotenv
load_dotenv()

from services.deployment_config import deploy
from services.brand_service import get_brand_settings
from services.notification_service import get_unread_count, mark_read
# ══ routes 包名冲突处理 ══
from auth_blueprint import register_auth
from routes.douyin_miniprogram import douyin_mp_bp

from models import get_db

# ── Edition 服务门控（单一事实源：deploy/editions/<edition>.yaml 的 services: 段）──
try:
    from agent_matrix.models import is_service_enabled
except Exception as _e:
    print(f'[Edition] ⚠️ is_service_enabled import failed, default all-enabled: {_e}')

    def is_service_enabled(_name):
        return True

# 移除 auth-center sys.path
_auth_center_norm = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'auth-center'))
sys.path = [p for p in sys.path if os.path.normpath(p) != _auth_center_norm]
_platform_dir = os.path.dirname(os.path.abspath(__file__))
if _platform_dir not in sys.path:
    sys.path.insert(0, _platform_dir)
sys.modules.pop('routes', None)

from main_site.routes.api_v1 import api_v1_bp
from main_site.routes.internal_api import internal_api_bp
# mini_program_bp 已解耦至插件 plugins/mini_app_builder/public_api.py（v2.0.0），
# 由 PluginManager mount_all_routes() 挂载（见下方 ── PluginManager ── 段）

from flask import (Flask, request, g, jsonify, render_template,
                   send_from_directory, redirect, Blueprint, Response, make_response)
import json
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)


@app.context_processor
def inject_deploy():
    return dict(deploy=deploy, edition=os.environ.get('VR_EDITION', ''))


# ══ 可观测性：request_id 注入与响应头 ══
# INT-004 接线：访问日志经统一 get_logger 输出（LOG_FILE 设置时落 JSON，自动并入 request_id/user_id）
from shared.logging import get_logger as _get_logger
_access_logger = _get_logger('verorun.access')


@app.before_request
def _inject_request_id():
    from shared.observability import init_request_id
    init_request_id()


@app.after_request
def _attach_request_id_header(response):
    from shared.observability import current_request_id
    response.headers['X-Request-Id'] = current_request_id()
    _access_logger.info('%s %s -> %s', request.method, request.path, response.status_code)
    return response


# ══ i18n ══
from i18n import _, get_lang, get_all_translations, resolve_locale

@app.before_request
def _i18n_resolve_locale():
    """请求级语言协商（规范 §5）：?lang= → Cookie lang → Accept-Language → 部署默认。"""
    g.lang_code = resolve_locale(
        lang_param=request.args.get('lang'),
        cookie=request.cookies.get('lang'),
        accept_header=request.headers.get('Accept-Language') or '',
    )

@app.context_processor
def inject_i18n():
    return {'_': _, 'lang': get_lang(), 'translations': get_all_translations(get_lang())}
app.jinja_env.globals['_'] = _


# ══ i18n 语言切换组件注入（i18n-standard §5）══
import functools
from flask import render_template_string


@functools.lru_cache(maxsize=2)
def _lang_switch_widget(lang):
    return render_template_string('{% include "_lang_switch.html" %}', lang=lang)


@app.after_request
def inject_lang_switch(response):
    """对 text/html 响应自动注入语言切换按钮（_lang_switch.html）。

    跳过条件：非 text/html、无 </body>、页面含 data-lang-switch-off、
    URL 带 no_lang_switch=1（插件 iframe 等特殊页面使用）。
    注入失败静默跳过，绝不影响页面本身。
    """
    try:
        if response.mimetype != 'text/html':
            return response
        data = response.get_data(as_text=True)
        if '</body>' not in data or 'data-lang-switch-off' in data:
            return response
        if request.args.get('no_lang_switch'):
            return response
        widget = _lang_switch_widget(get_lang())
        response.set_data(data.replace('</body>', widget + '</body>'))
    except Exception:
        pass
    return response


# AnalyticsMiddleware 仅在 Admin 服务启用，避免多进程 SQLite 写锁竞争
# from analytics.middleware import AnalyticsMiddleware
# AnalyticsMiddleware(app, service_name="platform")
app.config['TEMPLATES_AUTO_RELOAD'] = True
import jinja2
app.jinja_loader = jinja2.ChoiceLoader([
    app.jinja_loader,
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..')),
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..', 'plugins', 'shop', 'templates')),
])

# ── PluginManager ──
try:
    from plugin_manager.manager import PluginManager
    app.plugins_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'plugins')
    pm = PluginManager(app)
    # 技能注册中心（P0）：可用性求值 + 事件联动 + 注入器注册（开关关闭则回退旧行为）
    try:
        from plugin_manager.skill_registry import init_skill_registry
        init_skill_registry(pm)
    except Exception:
        pass
    # 启动期挂载全部已安装插件路由（含 mini_app_builder 的运行时 API
    # /api/v1/mini-program/*），运行时由门卫按启用状态放行/拦截
    # （Flask 3 运行时无法动态注册蓝图，故启动期全量挂载）。
    pm.mount_all_routes()

    @app.before_request
    def _plugin_gatekeeper():
        """禁用插件的路由请求返回 404，等价于插件不存在。"""
        from flask import abort
        if not pm.is_path_allowed(request.path):
            abort(404)

    print('[PluginManager] ✅ Platform 服务插件管理器已初始化')
except Exception as e:
    print(f'[PluginManager] ⚠️ Platform 服务初始化失败: {e}')

# ── Blueprint 注册 ──
register_auth(app, exclude_blueprints=['admin', 'cms_admin'])
app.register_blueprint(api_v1_bp)
app.register_blueprint(douyin_mp_bp)
app.register_blueprint(internal_api_bp)  # 内部服务 API（插件数据解耦后共享数据获取）
# mini_program_bp 已由 PluginManager 挂载（plugins/mini_app_builder）

# ── 旧用户中心路由重定向到 SPA（user_console 禁用时不注册）──
if is_service_enabled('user_console'):
    @app.route('/user-console/')
    @app.route('/user-console/<path:subpath>')
    def redirect_user_console(subpath=''):
        return redirect('/', 301)


@app.route('/orders')
def redirect_old_orders():
    return redirect('/', 301)


# ══════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════

def get_domain_config():
    """获取域名和Cookie配置信息"""
    host = request.headers.get('Host', '')
    try:
        brand = get_brand_settings()
        site_domain = brand.get('site_domain', '').strip()
    except Exception:
        site_domain = ''
    if not site_domain:
        site_domain = os.environ.get('DEPLOY_DOMAIN', '')
    host_name = host.split(':')[0].lower()
    cookie_domain = ('.' + site_domain) if site_domain else ('.' + host_name)
    platform_domain = f'platform.{site_domain}' if site_domain else f'platform.{host_name}'
    is_platform_host = (host_name == platform_domain or host_name == 'localhost'
                        or host_name.startswith('127.0.0.1') or host_name.startswith('192.168.'))
    return {
        'host_name': host_name,
        'site_domain': site_domain,
        'cookie_domain': cookie_domain,
        'platform_domain': platform_domain,
        'is_platform_host': is_platform_host,
    }


def handle_oauth_callback(domain_config):
    """处理OAuth登录回调：验证token、设置Cookie、重定向"""
    url_token = request.args.get('token')
    if not url_token:
        return None
    from services.jwt_service import validate_token
    payload = validate_token(url_token)
    if not payload:
        return None
    from urllib.parse import urlencode
    other_params = {k: v for k, v in request.args.items() if k != 'token'}
    target = request.path
    if other_params:
        target += '?' + urlencode(other_params, doseq=True)
    resp = make_response(redirect(target))
    is_secure = request.scheme == 'https'
    resp.set_cookie('sso_token', url_token, domain=domain_config['cookie_domain'],
                    path='/', max_age=604800, samesite='Lax', secure=is_secure, httponly=True)
    return resp


def _chatbot_context():
    """读取 AI Advisor 插件配置，供模板渲染使用。"""
    defaults = {
        'chatbot_enabled': True,
        'chatbot_title': 'AI Advisor',
        'chatbot_subtitle': 'Powered by AI Engine',
        'chatbot_welcome_message': 'Hello! I am your AI advisor. How can I help you today?',
        'chatbot_help_hint': 'Type help to see what I can do for you',
        'chatbot_avatar_url': '',
        'chatbot_agent_id': 'chat_assistant',
        'chatbot_max_history': 20,
        'chatbot_float_button_text': 'AI Advisor'
    }
    try:
        from shared.plugin_access import get_attr
        get_all_configs = get_attr('plugins.chatbot.models', 'get_all_configs',
                                   feature='platform_chatbot_context')
        if get_all_configs is None:
            return defaults
        cfg = get_all_configs('chatbot')
        return {
            'chatbot_enabled': str(cfg.get('enabled', '1')).lower() in ('1', 'true', 'yes', 'on'),
            'chatbot_title': cfg.get('title', defaults['chatbot_title']),
            'chatbot_subtitle': cfg.get('subtitle', defaults['chatbot_subtitle']),
            'chatbot_welcome_message': cfg.get('welcome_message', defaults['chatbot_welcome_message']),
            'chatbot_help_hint': cfg.get('help_hint', defaults['chatbot_help_hint']),
            'chatbot_avatar_url': cfg.get('avatar_url', defaults['chatbot_avatar_url']),
            'chatbot_agent_id': cfg.get('agent_id', defaults['chatbot_agent_id']),
            'chatbot_max_history': int(cfg.get('max_history', defaults['chatbot_max_history'])),
            'chatbot_float_button_text': cfg.get('float_button_text', defaults['chatbot_float_button_text']),
        }
    except Exception as e:
        print(f'[Platform] chatbot context failed: {e}')
        return defaults


def handle_platform_auth(domain_config):
    """处理平台域名认证，返回后台页面响应或重定向"""
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')

    payload = validate_token(token) if token else None
    if not payload:
        return redirect('/login?redirect=' + request.path)

    resp = make_response(render_template('index.html',
                                         site_domain=domain_config['site_domain'],
                                         server_token=token or '',
                                         **_chatbot_context()))
    is_secure = request.scheme == 'https'
    resp.set_cookie('sso_token', token, domain=domain_config['cookie_domain'],
                    path='/', max_age=604800, samesite='Lax', secure=is_secure, httponly=True)
    return resp


def _get_user_id_from_token():
    """从 JWT token 中提取 user_id"""
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    if not token:
        return None
    payload = validate_token(token)
    return payload.get('user_id') if payload else None


# ══════════════════════════════════════════════════════════════
# Routes — User Console
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """用户控制台首页 — 验证登录后渲染 SPA"""
    # 1. 处理 OAuth 回调
    if request.args.get('code'):
        domain_config = get_domain_config()
        oauth_response = handle_oauth_callback(domain_config)
        if oauth_response:
            return oauth_response

    # 2. 验证 token → 渲染控制台
    from services.jwt_service import validate_token
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if token:
        payload = validate_token(token)
        if payload:
            from services.brand_service import get_brand_settings
            brand = get_brand_settings() or {}
            if brand:
                brand['software_name'] = _('app_name')
            resp = make_response(render_template('index.html', brand=brand, server_token=token, **_chatbot_context()))
            site_domain = brand.get('site_domain', '').strip()
            cd = ('.' + site_domain) if site_domain else ''
            _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
            if cd:
                resp.set_cookie('sso_token', token, domain=cd, path='/',
                                max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
            else:
                resp.set_cookie('sso_token', token, path='/',
                                max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
            return resp

    # 3. 未登录 → 重定向到官网登录页
    site_url = deploy.url()
    from urllib.parse import quote
    return redirect(f'{site_url}/login?redirect=' + quote(request.url))


@app.route('/api-keys')
def api_keys_page():
    return redirect('/', 301)


# ══ 通知系统 API ══

@app.route('/api/notifications')
def user_notifications_list():
    """获取当前用户通知列表"""
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({'success': False, 'error': _('Please login first')}), 401
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
    except ValueError:
        limit, offset = 50, 0
    with get_db() as conn:
        total = conn.execute(
            'SELECT COUNT(*) as c FROM user_notifications WHERE user_id=%s', (user_id,)
        ).fetchone()['c']
        rows = conn.execute(
            'SELECT id, type, title, content, link_url, is_read, created_at '
            'FROM user_notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s',
            (user_id, limit, offset)
        ).fetchall()
    return jsonify({
        'success': True,
        'data': [dict(r) for r in rows],
        'total': total,
        'unread': get_unread_count(user_id)
    })


@app.route('/api/notifications/unread-count')
def user_notifications_unread():
    """获取未读通知数量"""
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({'success': False, 'error': _('Please login first')}), 401
    return jsonify({'success': True, 'count': get_unread_count(user_id)})


@app.route('/api/notifications/<int:nid>/read', methods=['PUT'])
def user_notification_mark_read(nid):
    """标记单条通知已读"""
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({'success': False, 'error': _('Please login first')}), 401
    ok = mark_read(user_id, nid)
    return jsonify({'success': ok})


@app.route('/api/notifications/read-all', methods=['PUT'])
def user_notifications_read_all():
    """标记全部通知已读"""
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({'success': False, 'error': _('Please login first')}), 401
    ok = mark_read(user_id)
    return jsonify({'success': ok})


@app.route('/api/notifications/<int:nid>', methods=['DELETE'])
def user_notification_delete(nid):
    """删除单条通知"""
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({'success': False, 'error': _('Please login first')}), 401
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM user_notifications WHERE user_id=%s AND id=%s', (user_id, nid))
            conn.commit()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True})


@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    """用户提交投诉/建议"""
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({'success': False, 'error': _('Please login first')}), 401
    data = request.get_json(silent=True) or {}
    fb_type = data.get('type', '').strip()
    category = data.get('category', '').strip()
    title = data.get('title', '').strip()
    content = data.get('content', '').strip()
    contact = data.get('contact', '').strip()

    if fb_type not in ('complaint', 'suggestion'):
        return jsonify({'success': False, 'error': _('Invalid type')}), 400
    if category not in ('功能问题', '内容问题', '账号问题', '支付问题', '其他'):
        return jsonify({'success': False, 'error': _('Invalid category')}), 400
    if not title or len(title) > 200:
        return jsonify({'success': False, 'error': _('Title must be 1-200 characters')}), 400
    if not content or len(content) > 5000:
        return jsonify({'success': False, 'error': _('Content must be 1-5000 characters')}), 400

    try:
        with get_db() as conn:
            conn.execute(
                'INSERT INTO user_feedback (user_id, type, category, title, content, contact) VALUES (%s,%s,%s,%s,%s,%s)',
                (user_id, fb_type, category, title, content, contact)
            )
            conn.commit()
        return jsonify({'success': True, 'message': _('Thank you for your feedback, we will handle it as soon as possible!')})
    except Exception as e:
        return jsonify({'success': False, 'error': _('Submission failed: {error}', error=str(e))}), 500


# ══ 静态文件 ══

@app.route('/static/media/<path:filename>')
def static_media(filename):
    """服务媒体库文件"""
    media_dir = os.path.join(os.path.dirname(__file__), '..', 'admin', 'static', 'media')
    return send_from_directory(media_dir, filename)


@app.route('/static/<path:filename>')
def static_files(filename):
    plat_path = os.path.join(os.path.dirname(__file__), 'static', filename)
    if os.path.isfile(plat_path):
        return send_from_directory(os.path.join(os.path.dirname(__file__), 'static'), filename)
    # 回退查 admin/static（处理 brand/ 等跨服务共享文件）
    admin_static = os.path.join(os.path.dirname(__file__), '..', 'admin', 'static')
    return send_from_directory(admin_static, filename)


# ══ 主题系统 ══
THEMES_ROOT_PLATFORM = os.path.join(os.path.dirname(__file__), '..', 'themes')


def _get_site_key_for_theme():
    return 'platform'


def _get_active_theme_slug_platform():
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'auth-center', 'routes'))
        from theme_admin import get_active_theme_slug_for_site
        return get_active_theme_slug_for_site(_get_site_key_for_theme())
    except Exception:
        return None


_theme_tpl_dir = None
_active_slug = _get_active_theme_slug_platform()
if _active_slug:
    _candidate = os.path.join(THEMES_ROOT_PLATFORM, _active_slug, 'templates')
    if os.path.isdir(_candidate):
        _theme_tpl_dir = _candidate
if _theme_tpl_dir:
    from jinja2 import ChoiceLoader, FileSystemLoader
    app.jinja_loader = ChoiceLoader([
        FileSystemLoader(_theme_tpl_dir),
        app.jinja_loader,
    ])


@app.context_processor
def inject_theme():
    slug = _get_active_theme_slug_platform()
    result = {}
    if slug and slug != 'default':
        result['theme_css_url'] = '/themes/{}/theme.css'.format(slug)
    else:
        result['theme_css_url'] = None
    try:
        result['brand'] = get_brand_settings()
    except:
        result['brand'] = None
    return result


@app.route('/themes/<slug>/<path:filename>')
def serve_theme_file(slug, filename):
    import re
    safe_slug = re.sub(r'[^a-z0-9\-]', '', slug.lower())
    if safe_slug != slug:
        return 'Invalid slug', 400
    theme_static = os.path.join(THEMES_ROOT_PLATFORM, slug)
    if not os.path.isdir(theme_static):
        return 'Theme not found', 404
    return send_from_directory(theme_static, filename)


@app.route('/health')
def health():
    """深度健康检查（可观测性）：PG 连通 + 服务基础状态。"""
    from shared.observability import measure, build_health_payload
    from models import get_db

    def _pg_ok():
        with get_db() as conn:
            conn.execute('SELECT 1')
        return True, 'ok'

    checks = [measure(_pg_ok, 'postgres')]
    payload = build_health_payload('platform', checks,
                                   extra={'version': getattr(app, 'version', '1.0.0')})
    return jsonify(payload)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8083
    app.run(host='0.0.0.0', port=port, debug=False)
