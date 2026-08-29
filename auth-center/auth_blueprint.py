#!/usr/bin/env python3
"""auth-center: Flask Blueprint Registration Helper"""
import sys, os

auth_dir = os.path.dirname(os.path.abspath(__file__))
models_dir = os.path.join(auth_dir, 'models')
# 确保 auth-center/models/ 在 sys.path 中优先级高于项目根目录，
# 避免根目录 models.py 冲突
for p in (models_dir, auth_dir):
    if p in sys.path:
        sys.path.remove(p)
sys.path.insert(0, models_dir)
sys.path.insert(0, auth_dir)
from models import init_db, get_db, DB_PATH
from services.session_service import TwoFactorRequired
from routes.auth import auth_bp
from routes.user import user_bp
try:
    from routes.payment import payment_bp
except ImportError:
    payment_bp = None
from routes.admin import admin_bp
from routes.cms_admin import cms_admin_bp
from routes.agents import agent_bp
from routes.sessions import session_bp


def register_auth(app, exclude_blueprints=None):
    """Mount auth blueprints on a Flask app."""
    try:
        init_db()
    except Exception as e:
        print(f'[DB] init_db warning: {e}')
    # Initialize authlib OAuth (via plugin)
    try:
        from plugins.oauth_config.services.oauth_service import init_oauth
        init_oauth(app)
        print('[OAuth] ✅ 插件 OAuth 已初始化')
    except Exception as e:
        print(f'[OAuth] ⚠️ 插件不可用: {e}')
    all_bps = [
        ('auth', auth_bp),
        ('user', user_bp),
        ('admin', admin_bp),
        ('cms_admin', cms_admin_bp),
        ('agent', agent_bp),
        ('session', session_bp),
    ]
    exclude = set(exclude_blueprints or [])
    for name, bp in all_bps:
        if name not in exclude:
            app.register_blueprint(bp)
    # ─── 插件系统由 PluginManager 统一管理 ───

    # 2FA 扩展点：登录签发被插件预检拦截（TwoFactorRequired）时，
    # 统一转为 needs_2fa 响应，前端据此跳转插件挑战页。app 级 errorhandler
    # 对所有已挂载 blueprint（含插件 OAuth 路由）生效。
    @app.errorhandler(TwoFactorRequired)
    def _handle_two_factor_required(e):
        from flask import jsonify
        return jsonify({'success': True, 'data': {
            'needs_2fa': True,
            'challenge_token': e.challenge_token,
            'redirect': e.redirect or '/',
        }})
    return app
