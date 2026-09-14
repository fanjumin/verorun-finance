"""auth-center/services/session_service.py
通用登录签发通道：所有登录方式（密码/短信/手机注册/邮箱注册/OAuth/抖音小程序）统一经过此函数。

2FA 扩展点（不承载任何 2FA 业务逻辑）：签发前调用通用 filter 钩子
`auth.before_issue_session`。插件（如 two_factor_auth）可注册该钩子决定
是否暂停签发（返回 {'blocked': True, ...} → 抛出 TwoFactorRequired）。
无插件订阅时 filter 原样返回，行为与不调用时完全一致（插件未启用即零影响）。
"""
import os
import hashlib
from flask import current_app
from services.jwt_service import create_token


class TwoFactorRequired(Exception):
    """登录签发被插件预检拦截（如要求第二因子），由 app 级 errorhandler 转成响应。"""

    def __init__(self, challenge_token=None, redirect='/'):
        super().__init__('two-factor authentication required')
        self.challenge_token = challenge_token
        self.redirect = redirect or '/'


def issue_auth_session(user_id, phone, app_name, is_admin=False, role='user',
                       user_info=None, device_name='Web Login', device_type='web',
                       scenario='login'):
    """统一登录签发入口。返回 {'blocked': False, 'token': <jwt>, 'user': {...}}。

    若插件 filter `auth.before_issue_session` 返回 {'blocked': True, ...}，
    抛出 TwoFactorRequired，由 app 级 errorhandler 返回 needs_2fa 响应。
    scenario='refresh'（token 续期）时插件不应要求第二因子。
    """
    _run_login_precheck(scenario, user_id, phone, app_name, is_admin, role,
                        user_info, device_name, device_type)
    # 从 admin_profiles 加载权限列表，注入 JWT（供插件级 _require_perm 校验）
    permissions = _load_user_permissions(user_id)
    token = create_token(user_id, phone=phone, app_name=app_name,
                         is_admin=is_admin, role=role, permissions=permissions)
    _write_user_session(user_id, token, device_name, device_type)
    return {'blocked': False, 'token': token,
            'user': {'id': user_id, 'phone': phone,
                     'is_admin': bool(is_admin), 'role': role,
                     'permissions': permissions,
                     **(user_info or {})}}


def _load_user_permissions(user_id):
    """从 admin_profiles 加载用户权限列表（JSON 数组）。
    非管理员或无 profile 时返回空列表。
    """
    import json
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT permissions FROM admin_profiles WHERE user_id=%s",
                (user_id,)
            ).fetchone()
        if row and row.get('permissions'):
            return json.loads(row['permissions'])
    except Exception:
        current_app.logger.exception('Failed to load user permissions')
    return []


def _run_login_precheck(scenario, user_id, phone, app_name, is_admin, role,
                        user_info, device_name, device_type):
    """执行通用登录预检 filter。

    插件注册的 filter 返回 {'blocked': True, ...} 时抛出 TwoFactorRequired；
    其余异常一律 fail-open（记录日志、原样继续签发），绝不停在登录。
    """
    try:
        from plugin_manager.hooks import get_hook_registry
        blocked = get_hook_registry().apply_filters(
            'auth.before_issue_session', None,
            scenario=scenario, user_id=user_id, phone=phone, app_name=app_name,
            is_admin=is_admin, role=role, user_info=user_info,
            device_name=device_name, device_type=device_type)
        if blocked and blocked.get('blocked'):
            raise TwoFactorRequired(
                challenge_token=blocked.get('challenge_token'),
                redirect=blocked.get('redirect', '/'))
    except TwoFactorRequired:
        raise
    except Exception:
        current_app.logger.exception(
            'auth.before_issue_session filter failed; fallback issue session')


def _write_user_session(user_id, token, device_name, device_type):
    """写 user_sessions（与各登录入口现有逻辑一致）。"""
    from models import get_db
    import flask
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ua = (flask.request.headers.get('User-Agent', '') or '')[:200]
    ip = flask.request.remote_addr or ''
    with get_db() as conn:
        conn.execute(
            "INSERT INTO user_sessions "
            "(user_id, token_hash, device_name, device_type, ip_address, user_agent, is_current, last_active, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,NOW(),NOW())",
            (user_id, token_hash, device_name, device_type, ip, ua))
        conn.commit()


def set_sso_cookie(resp, token, app_name='main'):
    """跨子域 SSO cookie（现有 4 份重复实现收敛到此）。

    app_name='admin' 时特权隔离（复审 §8.4）：不种主域共享 cookie，
    收窄到当前（admin 子）域，避免任一子域 XSS 读到管理员令牌。
    """
    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    # D-11: secure 标记按真实请求协议判定（优先 X-Forwarded-Proto，其次 request.scheme），
    # 避免纯 HTTP 部署下依赖 env DEPLOY_PROTOCOL 默认 https 导致 Secure cookie 被浏览器丢弃。
    try:
        from flask import request
        proto = (request.headers.get('X-Forwarded-Proto') or request.scheme or '').lower()
        is_https = proto == 'https'
    except Exception:
        is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
    if app_name == 'admin':
        resp.set_cookie('sso_token', token, path='/', max_age=604800,
                        samesite='Lax', secure=is_https, httponly=True)
    elif main_domain:
        resp.set_cookie('sso_token', token, domain='.' + main_domain,
                        path='/', max_age=604800, samesite='Lax',
                        secure=is_https, httponly=True)
    else:
        resp.set_cookie('sso_token', token, path='/', max_age=604800,
                        samesite='Lax', secure=is_https, httponly=True)
