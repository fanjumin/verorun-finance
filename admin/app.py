#!/usr/bin/env python3
# Force-load stdlib platform module before sys.path changes (prevents local platform/ shadowing)
import platform as _stdlib_platform
# VeroRun 维洛智能 (verorun.com / verorun.cn)
# 版权所有 (c) 2026 樊聚民 (fanjumin). All Rights Reserved.

"""Admin Panel — 管理后台 (独立端口 8084)"""
"""VeroRun — Multi-agent AI Content & Commerce Hub"""

import sys, os, re, secrets, time as _time, threading
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'auth-center'))
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, Response, g
from werkzeug.middleware.proxy_fix import ProxyFix
from models import init_db, get_db
from services.deployment_config import DeployConfig, deploy
from services.session_service import issue_auth_session
from routes.auth import auth_bp
from routes.admin import admin_bp
from routes.cms_admin import cms_admin_bp
from routes.user import user_bp
# social_bp（社媒推广）已解耦为 plugins/social_push/，由 PluginManager 挂载
from routes.social_media import social_media_bp
from routes.footer_admin import footer_bp
from routes.header_admin import header_bp
from routes.comments import comments_bp
from routes.theme_admin import theme_bp
from routes.cleaner_agent import cleaner_bp
from models.cms import init_cms_tables
from routes.douyin_miniprogram import douyin_mp_bp
from routes.knowledge_admin import knowledge_bp
import time as _time

# ── PluginManager ──
from plugin_manager.manager import PluginManager
from plugin_manager.routes import bp as plugin_bp
# captcha_bp 已迁移至插件 plugins/captcha_embedded/routes.py（2026-08-06，由 register_routes 挂载）

# ══ Simple in-memory rate limiter for captcha consume ══
_captcha_rate_limit = {}

def _check_rate_limit(key, max_per_minute=10):
    """Sliding window rate limit. Returns True if allowed."""
    now = _time.time()
    window = 60.0
    if key not in _captcha_rate_limit:
        _captcha_rate_limit[key] = []
    _captcha_rate_limit[key] = [t for t in _captcha_rate_limit[key] if now - t < window]
    if len(_captcha_rate_limit[key]) >= max_per_minute:
        return False
    _captcha_rate_limit[key].append(now)
    return True

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32)))
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

@app.context_processor
def inject_deploy():
    return dict(deploy=deploy, edition=_os.environ.get('VR_EDITION', ''))


# ══ i18n 国际化注入 ══
from i18n import _, get_lang, get_all_translations, resolve_locale
import os as _os

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
    return {'_': _, 'lang': get_lang(), 'translations': get_all_translations(get_lang()), 'MARKET': _os.environ.get('DEPLOY_MARKET', 'cn')}

app.jinja_env.globals['_'] = _


# ══ i18n 语言切换组件注入（i18n-standard §5）══
import functools
from flask import render_template_string


@functools.lru_cache(maxsize=2)
def _lang_switch_widget(lang):
    # 复用 main_site 共享组件（通过项目根 loader 解析，不复制文件）
    return render_template_string('{% include "main_site/templates/_lang_switch.html" %}', lang=lang)


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


@app.route('/api/v1/i18n/lang', methods=['GET', 'POST'])
def i18n_set_lang():
    """GET 返回当前语言；POST {lang} 写入 Cookie 并切换。仅支持 en / zh-CN。"""
    from i18n import _normalize_locale, SUPPORTED_LOCALES
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        lang = _normalize_locale(data.get('lang') or request.args.get('lang'))
        if not lang or lang not in SUPPORTED_LOCALES:
            return jsonify({'ok': False, 'error': _('Unsupported language')}), 400
        resp = jsonify({'ok': True, 'lang': lang})
        resp.set_cookie('lang', lang, max_age=365 * 24 * 3600, samesite='Lax')
        return resp
    return jsonify({'ok': True, 'lang': get_lang()})


# 添加项目根目录到模板搜索路径（统一页脚 _footer.html）
import jinja2
app.jinja_loader = jinja2.ChoiceLoader([
    app.jinja_loader,
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..', 'platform', 'templates')),
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..')),
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..', 'plugins', 'health_check', 'templates')),
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..', 'plugins', 'analytics', 'templates')),
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..', 'plugins', 'ads', 'templates')),
    jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), '..', 'plugins', 'shop', 'templates', 'admin')),
])

app.config['TEMPLATES_AUTO_RELOAD'] = False
app.jinja_env.auto_reload = False
# 文件系统模板字节码缓存 — worker 重启后无需重新编译模板
import tempfile, os as _os
_cache_dir = _os.path.join(tempfile.gettempdir(), 'jinja2_cache')
_os.makedirs(_cache_dir, exist_ok=True)
app.jinja_env.bytecode_cache = jinja2.FileSystemBytecodeCache(_cache_dir, '%s.cache')

try:
    init_db()
except Exception as e:
    print(f'[DB] init_db warning: {e}')

# ── i18n: 启动时从 YAML 播种到 DB（已移至 gunicorn_config.py post_fork） ──
# seed_from_yaml 通过 pg_try_advisory_lock + post_fork 延迟初始化，避免阻塞 worker
# 注册管理后台需要的 blueprint — 包含 user_bp（管理员基本设置 /user/config）
app.register_blueprint(user_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(cms_admin_bp)
# social_bp 由 PluginManager 挂载（plugins/social_push），此处不再注册
app.register_blueprint(social_media_bp)
app.register_blueprint(footer_bp)
app.register_blueprint(header_bp)
app.register_blueprint(comments_bp)
app.register_blueprint(theme_bp)
app.register_blueprint(douyin_mp_bp)  # Douyin Mini-Program API
app.register_blueprint(cleaner_bp)     # 数据清洗智能体
app.register_blueprint(knowledge_bp)   # 知识库管理后台
# 自动注册 Cleaner 为矩阵子 Agent
try:
    from routes.cleaner_agent import auto_register_sub_agent
    auto_register_sub_agent()
except Exception as e:
    print(f'[CleanerAgent] ⚠️ 自动注册失败: {e}')
# 启动知识库定期维护调度器（阶段三）
try:
    from routes.cleaner_agent import init_kb_scheduler
    init_kb_scheduler()
except Exception as e:
    print(f'[KnowledgeMaintenance] ⚠️ 调度器启动失败: {e}')

# ===== 自动续费引擎（插件订阅体系，唯一调度入口）=====
# 订阅系统已整体迁移至 plugins/subscription（subscription schema），
# 调度由插件 SUBSCRIPTION_JOBS 的 4 个任务接管。
try:
    from plugins.subscription.scheduler import run_renewal_scan, run_dunning_scan, check_expired_subscriptions, cleanup_old_orders
    from plugin_manager.subscription import run_plugin_sub_scan, run_plugin_sub_grace_scan
    from apscheduler.schedulers.background import BackgroundScheduler
    _renew_sched = BackgroundScheduler(timezone='Asia/Shanghai')
    _renew_sched.add_job(run_renewal_scan, 'cron', hour=2, minute=0)                 # 每日 02:00 扫描到期代扣
    _renew_sched.add_job(check_expired_subscriptions, 'cron', hour=2, minute=30)     # 每日 02:30 过期标记
    _renew_sched.add_job(run_dunning_scan, 'cron', hour=3, minute=0)                 # 每日 03:00 dunning 重试
    _renew_sched.add_job(cleanup_old_orders, 'cron', hour=3, minute=30)              # 每日 03:30 清理旧订单
    _renew_sched.add_job(run_plugin_sub_scan, 'cron', hour=2, minute=15)             # 每日 02:15 插件订阅到期扫描
    _renew_sched.add_job(run_plugin_sub_grace_scan, 'cron', hour=3, minute=15)       # 每日 03:15 插件订阅宽限期锁定
    _renew_sched.start()
    print('[Subscription] ✅ 插件订阅自动续费调度已启动（02:00 续费 / 02:15 插件到期 / 02:30 过期 / 03:00 dunning / 03:15 插件宽限 / 03:30 清理）')
except ImportError:
    print('[Subscription] ⚠️ APScheduler 未安装，订阅调度跳过')
except Exception as e:
    print(f'[Subscription] ⚠️ 订阅调度启动失败: {e}')

init_cms_tables()

# ===== PluginManager（新插件系统）=====
try:
    from version import __version__
    app.version = __version__
    app.plugins_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'plugins')
    pm = PluginManager(app)
    app.register_blueprint(plugin_bp)
    # captcha_bp 由插件 plugins/captcha_embedded 的 register_routes() 挂载（mount_all_routes）
    # 启动期挂载全部已安装插件（含 disabled）的路由，运行时由门卫按启用状态放行/拦截，
    # 从而实现后台启用/禁用插件免重启（Flask 3 运行时无法动态注册蓝图）。
    pm.mount_all_routes()

    @app.before_request
    def _plugin_gatekeeper():
        """禁用插件的路由请求返回 404，等价于插件不存在。"""
        from flask import request, abort
        if not pm.is_path_allowed(request.path):
            abort(404)

    print(f'[PluginManager] ✅ 管理 API 蓝图已注册 (/admin/plugins/*)')
except Exception as e:
    print(f'[PluginManager] ❌ 初始化失败: {e}')
    import traceback
    traceback.print_exc()

# ===== 自动化调度系统 (Cron + Workflow) =====
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'orchestrator'))
try:
    from orchestrator.routes import init_automation
    sched, worker = init_automation(app)
    app.config['AUTOMATION_SCHEDULER'] = sched
    app.config['AUTOMATION_WORKER'] = worker
    print(f'[Automation] ✅ 调度器 + Worker 池已初始化')
    print(f'[Automation] 📋 API: /admin/automation/*')
    # 插件自定义 DAG 节点注册（如 veroscholar_search），供工作流执行使用
    try:
        if worker is not None:
            _n = pm.register_all_dag_nodes(worker.workflow_engine)
            print(f'[PluginManager] ✅ 已注册 {_n} 个插件 DAG 节点')
    except Exception as e:
        print(f'[PluginManager] ⚠️ 插件 DAG 节点注册失败: {e}')
except ImportError as e:
    print(f'[Automation] ⚠️ 未安装 APScheduler: pip install apscheduler sqlalchemy')
    print(f'[Automation]    import error: {e}')
except Exception as e:
    print(f'[Automation] ❌ 初始化失败: {e}')
    import traceback
    traceback.print_exc()

# ===== Site Builder（已解耦为插件 plugins/site_builder，v2.1.0） =====
# 建站能力（提示词模板/建站任务/预览发布）与站点设置（设计令牌）已迁移至
# plugins/site_builder 插件，由 PluginManager 自动发现、安装并挂载路由
# （/admin/site-builder/*、/admin/site-settings/*），此处不再需要初始化。

# ===== Agent 矩阵系统 =====
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
try:
    from agent_matrix.routes import init_agent_matrix
    init_agent_matrix(app)
    print(f'[Agent Matrix] ✅ 已初始化')
    print(f'[Agent Matrix] 📋 API: /admin/agent-matrix/*')
except Exception as e:
    print(f'[Agent Matrix] ❌ 初始化失败: {e}')
    import traceback
    traceback.print_exc()

PLATFORM_STATIC = os.path.join(os.path.dirname(__file__), '..', 'platform', 'static')
ADMIN_STATIC = os.path.join(os.path.dirname(__file__), 'static')
ADS_STATIC = os.path.join(os.path.dirname(__file__), '..', 'plugins', 'ads', 'static')
_SHOP_PRODUCTS_STATIC = os.path.join(os.path.dirname(__file__), '..', 'plugins', 'shop', 'static', 'products')
os.makedirs(_SHOP_PRODUCTS_STATIC, exist_ok=True)
BRAND_STATIC = os.path.join(os.path.dirname(__file__), '..', 'static', 'brand')

# ══ 独立部署：订阅过期锁定（客户端模式，仅锁定后台管理页面） ══
if os.environ.get('APP_MODE', 'main') == 'client':
    try:
        from services.license_service import LicenseService as _LicenseService
        _ls = _LicenseService()

        @app.before_request
        def _check_subscription():
            """订阅过期时，管理后台页面跳转到续费页"""
            # 只锁定 /admin 开头的页面请求，不锁定 API
            path = request.path
            if not path.startswith('/admin'):
                return None
            # 静态文件不锁定
            if path.startswith('/admin/static/'):
                return None
            # 续费页不锁定
            if path == '/admin/renew' or path.startswith('/api/subscription'):
                return None
            # 仅检查页面路由（HTML展示），API调用不锁定
            if path.startswith('/admin/') and not path.startswith('/admin/api'):
                if not _ls.check_admin_access():
                    return redirect('/admin/renew')
            return None
        print('[License] ✅ 订阅过期检查已启用（客户端模式）')
    except Exception as e:
        print(f'[License] ⚠️ 订阅检查未启用: {e}')
else:
    print('[License] 主服务器模式，跳过订阅过期检查')

# ══ 域名白名单：仅允许配置的域名访问管理后台 ══
@app.before_request
def _check_admin_domain():
    """域名白名单：仅允许 system_config 中 admin_allowed_domains 配置的域名访问管理后台"""
    path = request.path
    if not path.startswith('/admin'):
        return None
    # 静态文件不锁定
    if path.startswith('/admin/static/'):
        return None
    # 登录页和登录 API 不锁定（否则无法进入设置页面配置域名）
    if path == '/admin/login' or path == '/admin' or path == '/':
        return None
    if path.startswith('/api/auth/'):
        return None
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT value FROM system_config WHERE key='admin_allowed_domains'"
        ).fetchone()
        conn.close()
        if row and row['value']:
            allowed = [d.strip().lower() for d in row['value'].split(',') if d.strip()]
            if allowed:
                host = request.host.split(':')[0].lower()
                if host not in allowed:
                    return jsonify({'success': False, 'error': 'Forbidden: admin access not allowed from this domain'}), 403
    except Exception:
        pass  # 数据库不可用时放行，避免锁死
    return None

@app.route('/')
def index():
    return redirect('/admin/login')


@app.route('/admin', strict_slashes=False)
def admin_page():
    """规范入口 — 先验证 is_admin，未登录跳 login"""
    from services.jwt_service import validate_token
    from flask import make_response
    token = request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return redirect('/admin/login')
    resp = make_response(render_template('admin.html', sso_token=token))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    # Set sso_token as HttpOnly cookie for reliable refresh auth
    resp.set_cookie('sso_token', token, max_age=86400*7, httponly=True,
                    secure=request.is_secure, samesite='Strict', path='/')
    return resp


@app.route('/admin/workflow-editor')
def workflow_editor():
    """工作流拖拽编辑器 — 独立 React 页面"""
    from services.jwt_service import validate_token
    from flask import make_response
    token = request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return redirect('/admin/login')
    resp = make_response(render_template('workflow_editor.html', sso_token=token))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/admin/login')
def admin_login_page():
    """管理员专用登录页 — 无验证码、无OAuth、支持三端（browser/desktop/mobile）"""
    # 如果已登录且有 admin 权限，直接跳到后台
    from services.jwt_service import validate_token
    token = request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token')
    payload = validate_token(token) if token else None
    if payload and payload.get('is_admin'):
        return redirect('/admin')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    """退出登录 — 清除服务端 HttpOnly cookie 后跳转登录页"""
    from flask import make_response
    resp = make_response(redirect('/admin/login'))
    for cn in ('sso_token', 'tm_token', 'token'):
        resp.set_cookie(cn, '', path='/', max_age=0)
    return resp


@app.route('/admin/login', methods=['POST'])
def admin_login_action():
    """管理员登录处理器
    支持两种方式:
      1. 密码登录 (所有客户端) — { username, password[, client_type] }
      2. 验证码登录 (桌面/移动) — { username, code[, client_type] }
         CN: 短信验证码, INTL: 邮箱验证码

    client_type: 'browser' (默认, Set-Cookie), 'desktop'/'mobile' (返回 JSON token)
    """
    import hashlib, hmac, time as _time_module, random, string
    from services.jwt_service import create_token

    data = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    code = (data.get('code') or '').strip()
    client_type = (data.get('client_type') or 'browser').strip().lower()
    ip = request.remote_addr or 'unknown'

    # ── IP 限流 ──
    now = int(_time_module.time())
    attempt_key = f'admin_login_{ip}'
    with _admin_login_lock:
        attempts = _admin_login_attempts.get(attempt_key, {'count': 0, 'first': now, 'banned_until': 0})
        if attempts.get('banned_until', 0) > now:
            remaining = attempts['banned_until'] - now
            return jsonify({'success': False, 'error': f'登录被临时锁定，{remaining // 60 + 1} 分钟后重试'}), 429
        if now - attempts['first'] > 900:
            attempts = {'count': 0, 'first': now, 'banned_until': 0}

    # ── 验证码登录分支 (桌面/移动端) ──
    if code:
        if not username:
            return jsonify({'success': False, 'error': '请输入账号'}), 400

        market = os.environ.get('DEPLOY_MARKET', 'cn')
        now_iso = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        from models import get_db

        with get_db() as conn:
            if market == 'intl':
                # 邮箱验证码登录
                row = conn.execute(
                    "SELECT * FROM email_codes WHERE email=%s AND code=%s AND purpose='login' AND used=0 AND expires_at>%s ORDER BY id DESC LIMIT 1",
                    (username, code, now_iso)
                ).fetchone()
                if not row:
                    _hit_attempt(attempts, now)
                    return jsonify({'success': False, 'error': '验证码错误或已过期'}), 400
                row = dict(row)
                if row['attempts'] >= 5:
                    return jsonify({'success': False, 'error': '尝试次数过多，请重新获取验证码'}), 400
                conn.execute('UPDATE email_codes SET used=1 WHERE id=%s', (row['id'],))
                # Find user by email
                user = conn.execute('SELECT * FROM users WHERE email=%s AND is_admin=1', (username,)).fetchone()
            else:
                # 短信验证码登录 — 复用 sms_codes 表
                row = conn.execute(
                    "SELECT * FROM sms_codes WHERE phone=%s AND code=%s AND purpose='login' AND used=0 AND expires_at>%s ORDER BY id DESC LIMIT 1",
                    (username, code, now_iso)
                ).fetchone()
                if not row:
                    _hit_attempt(attempts, now)
                    return jsonify({'success': False, 'error': '验证码错误或已过期'}), 400
                row = dict(row)
                if row['attempts'] >= 5:
                    return jsonify({'success': False, 'error': '尝试次数过多，请重新获取验证码'}), 400
                conn.execute('UPDATE sms_codes SET used=1 WHERE id=%s', (row['id'],))
                # Find user by phone
                user = conn.execute('SELECT * FROM users WHERE phone=%s AND is_admin=1', (username,)).fetchone()

            conn.commit()

        if not user:
            with _admin_login_lock:
                attempts['count'] += 1
                _admin_login_attempts[attempt_key] = attempts
            return jsonify({'success': False, 'error': 'Account not found or not an admin account'}), 400

        user = dict(user)
        # Query admin role
        admin_role = 'user'
        try:
            with get_db() as conn:
                prof = conn.execute('SELECT role FROM admin_profiles WHERE user_id=%s', (user['id'],)).fetchone()
                if prof:
                    admin_role = prof['role']
        except Exception:
            pass
        with _admin_login_lock:
            _admin_login_attempts.pop(attempt_key, None)
        _res = issue_auth_session(user['id'], phone=user.get('phone'), app_name='admin',
                                  is_admin=True, role=admin_role)
        token = _res['token']
        _log_admin_action(user['id'], 'login_success_code', ip, f'user={username} client={client_type}')

        return _make_login_response(token, client_type)

    # ── 密码登录分支 (浏览器/所有端) ──
    if not username or not password:
        return jsonify({'success': False, 'error': '请输入账号和密码'}), 400

    from models import get_db
    with get_db() as conn:
        user = conn.execute(
            'SELECT id, username, phone, password_hash, is_admin, display_name FROM users WHERE (username=%s OR phone=%s) AND is_admin=1',
            (username, username)
        ).fetchone()

    if not user:
        with _admin_login_lock:
            attempts['count'] += 1
            if attempts['count'] >= 5:
                attempts['banned_until'] = now + 1800
            _admin_login_attempts[attempt_key] = attempts
        _log_admin_action(None, 'login_failed', ip, f'user={username} not_found')
        return jsonify({'success': False, 'error': 'Account not found or not an admin account'}), 400

    stored = user['password_hash']
    if not stored:
        with _admin_login_lock:
            attempts['count'] += 1
            _admin_login_attempts[attempt_key] = attempts
        return jsonify({'success': False, 'error': '该账号未设置密码，请使用验证码登录'}), 400

    pw_ok = False
    parts = stored.split(':')
    if len(parts) == 5 and parts[0] == 'pbkdf2' and parts[1] == 'sha256':
        iterations = int(parts[2])
        salt = parts[3]
        pw_hash = parts[4]
        check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations).hex()
        pw_ok = hmac.compare_digest(pw_hash, check)
    else:
        try:
            from werkzeug.security import check_password_hash
            pw_ok = check_password_hash(stored, password)
        except Exception:
            pass

    if not pw_ok:
        with _admin_login_lock:
            attempts['count'] += 1
            if attempts['count'] >= 5:
                attempts['banned_until'] = now + 1800
            _admin_login_attempts[attempt_key] = attempts
        _log_admin_action(user['id'], 'login_failed', ip, f'user={username} bad_password')
        return jsonify({'success': False, 'error': '密码错误'}), 400

    with _admin_login_lock:
        _admin_login_attempts.pop(attempt_key, None)
    # Query admin role
    admin_role = 'user'
    try:
        with get_db() as conn:
            prof = conn.execute('SELECT role FROM admin_profiles WHERE user_id=%s', (user['id'],)).fetchone()
            if prof:
                admin_role = prof['role']
    except Exception:
        pass
    _res = issue_auth_session(user['id'], phone=user['phone'], app_name='admin',
                              is_admin=True, role=admin_role)
    token = _res['token']
    _log_admin_action(user['id'], 'login_success', ip, f'user={username} client={client_type}')

    return _make_login_response(token, client_type)


def _make_login_response(token, client_type):
    """根据客户端类型返回不同的响应格式"""
    from flask import make_response
    resp = make_response(jsonify({'success': True, 'data': {'token': token}}))
    if client_type in ('desktop', 'mobile'):
        # 桌面/移动端：只返回 JSON，不设 cookie
        return resp
    # 浏览器：Set-Cookie
    resp.set_cookie('sso_token', token, max_age=86400*7, httponly=True,
                    secure=request.is_secure, samesite='Strict', path='/')
    return resp


def _hit_attempt(attempts, now):
    """记录一次失败尝试"""
    attempts['count'] += 1
    if attempts['count'] >= 5:
        attempts['banned_until'] = now + 1800


@app.route('/admin/login/send-code', methods=['POST'])
def admin_send_code():
    """发送桌面/移动端验证码
    CN: 短信验证码  INTL: 邮箱验证码
    """
    import hashlib, hmac, time as _time_module, random, string, secrets
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get('phone') or data.get('email') or '').strip()
    ip = request.remote_addr or 'unknown'

    if not target:
        return jsonify({'success': False, 'error': '请输入手机号或邮箱'}), 400

    # IP 频控：每分钟最多 2 次
    code_key = f'code_{ip}'
    now = int(_time_module.time())
    with _admin_login_lock:
        last = _admin_login_attempts.get(code_key, 0)
        if now - last < 60:
            remaining = 60 - (now - last)
            return jsonify({'success': False, 'error': f'发送过于频繁，请 {remaining} 秒后再试'}), 429
        _admin_login_attempts[code_key] = now

    market = os.environ.get('DEPLOY_MARKET', 'cn')
    code = ''.join(secrets.choice(string.digits) for _ in range(6))
    now_iso = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    expires_ts = __import__('datetime').datetime.now() + __import__('datetime').timedelta(minutes=5)
    expires_at = expires_ts.strftime('%Y-%m-%d %H:%M:%S')

    # Check user exists AND is admin
    from models import get_db
    with get_db() as conn:
        if market == 'intl':
            user = conn.execute('SELECT id FROM users WHERE email=%s AND is_admin=1', (target,)).fetchone()
        else:
            user = conn.execute('SELECT id FROM users WHERE phone=%s AND is_admin=1', (target,)).fetchone()

    if not user:
        return jsonify({'success': False, 'error': 'Account not found or not an admin account'}), 400

    if market == 'intl':
        # 邮箱验证码：存表 + 发邮件
        from models import get_db
        with get_db() as conn:
            conn.execute(
                'INSERT INTO email_codes (email, code, purpose, expires_at) VALUES (%s,%s,%s,%s)',
                (target, code, 'login', expires_at))
            conn.commit()

        try:
            from plugins.email.services import send_email
            send_email(
                to_addr=target,
                subject='VeroRun Admin Login Code',
                body_text=f'Your verification code is: {code}\n\nValid for 5 minutes.\n\nIf you did not request this, please ignore.'
            )
            print(f'[Admin] Email code sent to {target}')
        except Exception as e:
            print(f'[Admin] Email send failed: {e} (stub: {code})')
        return jsonify({'success': True, 'message': '验证码已发送到邮箱'})
    else:
        # 短信验证码：委托给 /auth/sms/send
        from models import get_db
        with get_db() as conn:
            conn.execute(
                'INSERT INTO sms_codes (phone, code, purpose, expires_at) VALUES (%s,%s,%s,%s)',
                (target, code, 'login', expires_at))
            conn.commit()

        try:
            # Try SmsPlugin first
            import flask as _flask
            _pm = _flask.current_app.extensions.get('plugin_manager')
            _sms = _pm.get_instance('sms') if (_pm and _pm.is_enabled('sms')) else None
            if _sms:
                _sms.send_sms(target, code, 'login')
            else:
                from services.sms_service import send_sms
                send_sms(target, code, 'login')
        except Exception:
            pass  # stub mode already prints
        return jsonify({'success': True, 'message': '验证码已发送'})


# ── 内存限流存储（服务重启后清空，可接受）──
_admin_login_attempts = {}
# 审计 PERF-001：gthread 多线程下保护内存限流 dict 的原子读写（sync worker 下无需）
_admin_login_lock = threading.Lock()


def _log_admin_action(admin_id, action, ip, detail=''):
    """记录管理员操作日志 — 异步写入，不阻塞响应"""
    import threading
    def _write():
        from models import get_db as _gdb
        try:
            with _gdb() as conn:
                conn.execute(
                    'INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail, ip_address) VALUES (%s,%s,%s,%s,%s,%s)',
                    (admin_id or 0, action, 'admin', 'login', detail, ip)
                )
                conn.commit()
        except Exception:
            pass
    threading.Thread(target=_write, daemon=True).start()


@app.route('/api/admin/first-password-set', methods=['POST'])
def first_password_set():
    """首次登录强制修改密码（无需短信验证）。仅当 password_changed_at IS NULL 时可用。"""
    from services.jwt_service import validate_token
    import hashlib, secrets as _secrets
    data = request.get_json() or {}
    token = (request.headers.get('Authorization', '') or '').replace('Bearer ', '')
    password = (data.get('password') or '').strip()
    if not token or not password:
        return jsonify({'success': False, 'error': 'Missing token or password'}), 400
    if len(password) < 10:
        return jsonify({'success': False, 'error': 'Password must be at least 10 characters'}), 400
    payload = validate_token(token)
    if not payload:
        return jsonify({'success': False, 'error': 'Invalid or expired token'}), 401
    from models import get_db
    with get_db() as conn:
        user = conn.execute('SELECT id, password_changed_at FROM users WHERE id=%s AND is_admin=1', (payload['user_id'],)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'Admin user not found'}), 404
        if user['password_changed_at'] is not None:
            return jsonify({'success': False, 'error': 'Password already set, use /user/password/set'}), 400
        from datetime import datetime as _dt
        salt = _secrets.token_hex(16)
        pw_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
        stored = f'pbkdf2:sha256:600000:{salt}:{pw_hash}'
        now_str = _dt.utcnow().isoformat()
        conn.execute('UPDATE users SET password_hash=%s, password_changed_at=%s WHERE id=%s', (stored, now_str, payload['user_id']))
        conn.commit()
    return jsonify({'success': True, 'message': 'Password set, please login again'}), 200


@app.route('/reset-password')
def reset_password_page():
    return render_template('reset_password.html')


@app.route('/health')
def health():
    """深度健康检查（P0-3）：DB 连通性 + 关键表 + 插件系统状态；异常返回 503。

    供探活系统（health_service / 外部监控）真实判定 admin 服务健康度，
    而非仅返回固定 ok。异常详情不外泄（仅记录错误类型）。
    """
    checks = {}
    degraded = []
    # ① DB 连通性（单独判定，失败不归因到其他检查；含 import 异常兜底）
    try:
        from plugin_manager.models import get_registry_db
        with get_registry_db() as conn:
            conn.execute('SELECT 1').fetchone()
        checks['db'] = 'ok'
    except Exception:
        checks['db'] = 'error'
        degraded.append('db connectivity error')
    # ② 关键表 + 插件/商店状态（独立 try，异常归因为检查失败而非 db）
    try:
        with get_registry_db() as conn:
            for tbl in ('plugin_registry', 'store_plugins', 'plugin_submissions'):
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s", (tbl,)
                ).fetchone()
                exists = bool(row and row['c'])
                checks[f'table.{tbl}'] = exists
                if not exists:
                    degraded.append(f'missing table {tbl}')
            def _cnt(sql):
                r = conn.execute(sql).fetchone()
                return int(r['c']) if r else 0
            checks['plugins_enabled'] = _cnt(
                "SELECT COUNT(*) AS c FROM plugin_registry "
                "WHERE status IN ('enabled','active')")
            checks['plugins_error'] = _cnt(
                "SELECT COUNT(*) AS c FROM plugin_registry WHERE status='error'")
            if checks['plugins_error']:
                degraded.append(f"{checks['plugins_error']} plugin(s) in ERROR state")
            checks['store_plugins_count'] = _cnt('SELECT COUNT(*) AS c FROM store_plugins')
    except Exception as e:
        degraded.append(f'plugin status check failed: {type(e).__name__}')

    status = 'ok' if not degraded else 'degraded'
    code = 200 if status == 'ok' else 503
    return jsonify({
        'status': status,
        'service': 'admin-panel',
        'port': 8084,
        'checks': checks,
        'issues': degraded,
    }), code

def _is_plugin_embed_path(path: str) -> bool:
    """判断 path 是否为已注册插件 menu 声明的 embed_url 路径。

    仅用于 SPA catch-all 护栏：命中且到达此处 = 插件蓝图未服务该路径，
    返回 404 而不是渲染 admin 壳（否则插件 iframe 内嵌套管理面板 → 重叠+无限递归）。
    """
    try:
        import json as _json
        from plugin_manager.models import get_registry_db
        with get_registry_db() as conn:
            rows = conn.execute('SELECT metadata FROM plugin_registry').fetchall()
        for row in rows:
            meta = row['metadata']
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta or '{}')
                except Exception:
                    continue
            menu = (meta or {}).get('menu') or {}
            items = menu.get('items') or []
            if not items and menu.get('embed_url'):
                items = [menu]  # 兼容旧单数格式
            for it in items:
                u = (it.get('embed_url') or '').split('?')[0].rstrip('/')
                if u and (path == u or path.startswith(u + '/')):
                    return True
    except Exception:
        pass
    return False


@app.route('/admin/<path:subpath>')
def admin_spa_catchall(subpath):
    """SPA catch-all — /admin/xxx 全部渲染 admin SPA 壳，前端根据 pathname 路由"""
    from flask import abort
    # 防递归护栏：已注册插件 menu 的 embed_url 路径若未被插件蓝图服务，
    # 直接 404，禁止回落 admin SPA 壳（避免 iframe 嵌套管理面板 → 重叠+无限递归）
    if _is_plugin_embed_path(request.path):
        abort(404)
    from services.jwt_service import validate_token
    from flask import make_response
    token = request.args.get('token') or request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return redirect('/admin/login')
    resp = make_response(render_template('admin.html', sso_token=token))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.set_cookie('sso_token', token, max_age=86400*7, httponly=True,
                    secure=request.is_secure, samesite='Strict', path='/')
    return resp


@app.route('/avatar/gen/<path:seed>')
def generated_avatar(seed):
    """生成默认首字母头像 SVG（无自定义头像时使用）"""
    from services.avatar_service import generate_initials_svg
    svg = generate_initials_svg(seed)
    return Response(svg, mimetype='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/api/social-links')
def public_social_links():
    """公开 API：页脚社媒图标列表（旧表 social_links）"""
    from models import get_db
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, url, icon_url, platform, sort_order '
            'FROM social_links WHERE is_active=1 ORDER BY sort_order ASC, id ASC'
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@app.route('/api/social-media')
def public_social_media():
    """公开 API：社媒图标列表（新表 social_media_links）"""
    from models import get_db
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, platform_name, icon_type, icon_value, url, hover_text '
            'FROM social_media_links WHERE is_enabled=1 ORDER BY display_order ASC, id ASC'
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@app.route('/api/interests')
def public_interests():
    """公开 API：兴趣标签列表（按分类分组），支持 ?search= 模糊搜索"""
    from models import get_db
    search = request.args.get('search', '').strip()
    with get_db() as conn:
        if search:
            rows = conn.execute(
                'SELECT id, name, category, is_hot FROM interests WHERE is_active=1 AND is_hot=1 AND name LIKE %s ORDER BY category, sort_order, id',
                ('%'+search+'%',)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, name, category, is_hot FROM interests WHERE is_active=1 AND is_hot=1 ORDER BY category, sort_order, id'
            ).fetchall()
    grouped = {}
    for r in rows:
        d = dict(r)
        cat = d['category']
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(d)
    return jsonify({'success': True, 'data': grouped, 'categories': list(grouped.keys())})


@app.route('/static/<path:filename>')
def static_files(filename):
    """优先从 admin/static/ 读取，回退到 platform/static/"""
    local_path = os.path.join(ADMIN_STATIC, filename)
    if os.path.isfile(local_path):
        return send_from_directory(ADMIN_STATIC, filename)
    return send_from_directory(PLATFORM_STATIC, filename)


@app.route('/admin/static/<path:filename>')
def admin_static_files(filename):
    """Nginx 代理 /admin/ → 8084 后，/admin/static/ 路径也需处理"""
    return static_files(filename)


@app.route('/static/brand/<path:filename>')
def brand_static_files(filename):
    """品牌静态文件（favicon 等）"""
    return send_from_directory(BRAND_STATIC, filename)


@app.route('/static/ads/<path:filename>')
def ads_static_files(filename):
    """广告插件静态文件"""
    return send_from_directory(ADS_STATIC, filename)


@app.route('/shop/static/products/<path:filename>')
def shop_product_images(filename):
    """商品图片 — 从插件静态目录提供"""
    return send_from_directory(_SHOP_PRODUCTS_STATIC, filename)




# ══ 主题系统: Jinja2 模板覆盖 + theme.css 注入 ══
THEMES_ROOT_ADMIN = os.path.join(os.path.dirname(__file__), '..', 'themes')

# 主题 slug 缓存（60秒 TTL）
_theme_slug_cache = {'value': None, 'ts': 0}

def _get_active_theme_slug_admin():
    """获取当前激活的主题 slug（带 60 秒 TTL 缓存）"""
    now = _time.perf_counter()
    if now - _theme_slug_cache['ts'] < 60:
        return _theme_slug_cache['value']
    try:
        from routes.theme_admin import get_active_theme_slug_for_site
        slug = get_active_theme_slug_for_site('admin')
    except Exception:
        slug = None
    _theme_slug_cache['value'] = slug
    _theme_slug_cache['ts'] = now
    return slug

# Jinja2 ChoiceLoader: 优先从激活主题的 templates/ 目录加载
theme_tpl_dir = None
active_slug = _get_active_theme_slug_admin()
if active_slug:
    candidate = os.path.join(THEMES_ROOT_ADMIN, active_slug, 'templates')
    if os.path.isdir(candidate):
        theme_tpl_dir = candidate
if theme_tpl_dir:
    from jinja2 import ChoiceLoader, FileSystemLoader
    app.jinja_loader = ChoiceLoader([
        FileSystemLoader(theme_tpl_dir),
        app.jinja_loader,
    ])


@app.context_processor
def inject_theme():
    """注入 theme_css_url + brand 到所有模板"""
    slug = _get_active_theme_slug_admin()
    result = {}
    if slug and slug != 'default':
        result['theme_css_url'] = '/themes/{}/theme.css'.format(slug)
    else:
        result['theme_css_url'] = None
    try:
        from services.brand_service import get_brand_settings
        result['brand'] = get_brand_settings()
        if result['brand']:
            from i18n import _ as _i18n
            result['brand']['software_name'] = _i18n('app_name')
    except:
        result['brand'] = None
    return result


@app.route('/themes/<slug>/<path:filename>')
def serve_theme_file(slug, filename):
    """公开访问主题静态文件"""
    import re
    safe_slug = re.sub(r'[^a-z0-9\-]', '', slug.lower())
    if safe_slug != slug:
        return 'Invalid slug', 400
    theme_static = os.path.join(THEMES_ROOT_ADMIN, slug)
    if not os.path.isdir(theme_static):
        return 'Theme not found', 404
    return send_from_directory(theme_static, filename)


# ── 插件 404 兜底：插件未启用时返回空 JSON ──
PLUGIN_FALLBACK_PATHS = [
    '/admin/social/',
    '/admin/channels/',
    '/plugin/coupons/',
]

@app.errorhandler(404)
def plugin_fallback_404(e):
    for prefix in PLUGIN_FALLBACK_PATHS:
        if request.path.startswith(prefix):
            return '<html><body style="background:#0d1117;color:#8b949e;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;margin:0"><div style="text-align:center"><h2 style="color:#f85149">Plugin Not Available</h2><p>This plugin is currently disabled or not installed.</p></div></body></html>', 200, {'Content-Type': 'text/html; charset=utf-8'}
    # Non-plugin 404 — return plain text
    return 'Not Found', 404


@app.route('/admin/api/check-update', methods=['GET'])
def admin_check_update():
    """Check for new version: compare local VERSION against remote git tags.
    Cached for 5 minutes to avoid hammering the remote on every page load."""
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '') or request.cookies.get('sso_token', '')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    from version import get_version

    # In-memory cache (60 seconds — shorter cache so new commits are detected quickly)
    now = _time.time()
    cached = getattr(admin_check_update, '_cache', None)
    if cached and (now - cached['ts']) < 60:
        return jsonify(cached['data'])

    local_ver = get_version()
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _subprocess = __import__('subprocess')

    # ── Strategy: compare local HEAD vs remote master commit hash ──
    # This detects new updates even when no git tag is pushed.
    local_commit = ''
    remote_commit = ''
    commit_diff = False
    try:
        r = _subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=_project_root, capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            local_commit = r.stdout.strip()
    except Exception:
        pass

    try:
        r = _subprocess.run(
            ['git', 'ls-remote', 'origin', 'master'],
            cwd=_project_root, capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            # Output format: "<commit_hash>\trefs/heads/master"
            remote_commit = r.stdout.split()[0].strip()
    except Exception:
        pass

    if local_commit and remote_commit:
        commit_diff = (local_commit != remote_commit)

    # ── Also fetch latest semver tag (for display purposes) ──
    latest_tag_ver = local_ver
    try:
        r = _subprocess.run(
            ['git', 'ls-remote', '--tags', 'origin'],
            cwd=_project_root, capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            tags = re.findall(r'refs/tags/v(\d+\.\d+\.\d+)$', r.stdout, re.M)
            if tags:
                tags.sort(key=lambda v: [int(x) for x in v.split('.')])
                latest_tag_ver = tags[-1]
    except Exception:
        pass

    # Semantic version comparison
    def _cmp(v1, v2):
        a = [int(x) for x in v1.split('.')]
        b = [int(x) for x in v2.split('.')]
        for i in range(max(len(a), len(b))):
            av = a[i] if i < len(a) else 0
            bv = b[i] if i < len(b) else 0
            if av != bv:
                return av - bv
        return 0

    # has_update if remote commit differs OR a newer tag exists
    has_update = commit_diff or _cmp(latest_tag_ver, local_ver) > 0

    # Display version: prefer latest tag if newer than local; otherwise show remote commit short hash
    if _cmp(latest_tag_ver, local_ver) > 0:
        latest_ver = latest_tag_ver
    elif commit_diff and remote_commit:
        latest_ver = f'{local_ver}+{remote_commit[:7]}'
    else:
        latest_ver = local_ver

    data = {
        'success': True,
        'current': local_ver,
        'latest': latest_ver,
        'has_update': has_update,
        'local_commit': local_commit[:7] if local_commit else '',
        'remote_commit': remote_commit[:7] if remote_commit else '',
    }
    admin_check_update._cache = {'ts': now, 'data': data}
    return jsonify(data)


@app.route('/admin/api/update', methods=['POST'])
def admin_update():
    """One-click update: git pull → pip install → restart services.
    Output is streamed to a log file and status is tracked via a JSON status file
    so the admin UI can poll for real-time progress."""
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '') or request.cookies.get('sso_token', '')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return jsonify({'success': False, 'error': _('Unauthorized')}), 401

    import json, subprocess, threading, time

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # /run/verorun/ — managed by systemd RuntimeDirectory (see deploy/install.sh).
    # tmpfs: cleared on reboot, owned by APP_USER, no root-permission conflicts.
    _log_dir = '/run/verorun'
    _log_file = os.path.join(_log_dir, 'update.log')
    _status_file = os.path.join(_log_dir, 'update_status.json')

    # Reject if an update is already running
    if os.path.exists(_status_file):
        try:
            with open(_status_file, 'r') as f:
                _prev = json.load(f)
            if _prev.get('status') == 'running':
                return jsonify({'success': False, 'error': _('An update is already in progress')}), 409
        except Exception:
            pass

    # Write initial status
    os.makedirs(_log_dir, exist_ok=True)
    _status = {'status': 'running', 'progress': 0, 'message': 'Starting update...', 'error': None}
    with open(_status_file, 'w') as f:
        json.dump(_status, f)

    def _write_status(status, progress, message, error=None):
        with open(_status_file, 'w') as f:
            json.dump({'status': status, 'progress': progress, 'message': message, 'error': error}, f)

    def _do_update():
        try:
            with open(_log_file, 'a') as log:
                log.write(f"\n=== Update started at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                log.flush()

                install_sh = os.path.join(_project_root, 'deploy', 'install.sh')
                _write_status('running', 10, 'Fetching updates from remote...')

                proc = subprocess.Popen(
                    ['sudo', 'bash', install_sh, 'update'],
                    cwd=_project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True
                )

                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    # Update progress based on recognizable output markers
                    lower = line.lower()
                    if 'git fetch' in lower:
                        _write_status('running', 20, 'Fetching updates from remote...')
                    elif 'git merge' in lower or 'git reset' in lower:
                        _write_status('running', 40, 'Applying updates...')
                    elif 'pip install' in lower or 'pip3 install' in lower:
                        _write_status('running', 60, 'Installing dependencies...')
                    elif 'restart' in lower or 'systemctl' in lower:
                        _write_status('running', 80, 'Restarting services...')

                proc.wait()

                log.write(f"\n=== Update finished at {time.strftime('%Y-%m-%d %H:%M:%S')} exit_code={proc.returncode} ===\n")
                log.flush()

                if proc.returncode == 0:
                    _write_status('success', 100, 'Update completed successfully')
                else:
                    _write_status('failed', 100, 'Update failed', f'exit_code={proc.returncode}')
        except Exception as e:
            _write_status('failed', 0, 'Update failed', str(e))
            try:
                with open(_log_file, 'a') as log:
                    log.write(f"[ERROR] {e}\n")
                    log.flush()
            except Exception:
                pass

    # Clear version-check cache so next load fetches fresh data
    admin_check_update._cache = None
    threading.Thread(target=_do_update, daemon=True).start()

    return jsonify({'success': True, 'message': _('Update started'), 'log_file': _log_file})


@app.route('/admin/api/update-status', methods=['GET'])
def admin_update_status():
    """Poll update progress from the status file."""
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '') or request.cookies.get('sso_token', '')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return jsonify({'success': False, 'error': _('Unauthorized')}), 401

    import json, os as _os
    _status_file = '/run/verorun/update_status.json'

    if not _os.path.exists(_status_file):
        return jsonify({'success': True, 'status': 'idle', 'progress': 0, 'message': 'No update running'})

    with open(_status_file, 'r') as f:
        _status = json.load(f)
    return jsonify({'success': True, **_status})


def _verify_frontend_lib_integrity():
    """Part A: 校验 admin/static/lib/ 前端库文件哈希与 MANIFEST.json 是否一致。

    只记录告警，不阻断启动。清单由 admin/static/lib/MANIFEST.json 维护，
    升级库文件后必须同步更新对应 sha256，否则将产生误报。
    """
    import json
    import hashlib as _hashlib
    _manifest_path = os.path.join(os.path.dirname(__file__), 'static', 'lib', 'MANIFEST.json')
    if not os.path.exists(_manifest_path):
        print('[Integrity] MANIFEST.json not found, skip frontend lib check')
        return
    try:
        with open(_manifest_path, 'r', encoding='utf-8') as f:
            _manifest = json.load(f)
    except Exception as e:
        print(f'[Integrity] MANIFEST.json parse failed: {e}')
        return
    for entry in _manifest.get('libraries', []):
        _rel = entry.get('path', '')
        _abs = os.path.join(os.path.dirname(_manifest_path), _rel)
        _expected = entry.get('sha256', '')
        if not os.path.exists(_abs):
            print(f'[Integrity] WARN frontend lib missing: {_rel}')
            continue
        with open(_abs, 'rb') as f:
            _actual = _hashlib.sha256(f.read()).hexdigest()
        if _actual != _expected:
            print(f'[Integrity] WARN frontend lib MODIFIED: {_rel} (sha256 mismatch)')


if __name__ == '__main__':
    _verify_frontend_lib_integrity()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8084
    app.run(host='0.0.0.0', port=port, debug=False)
