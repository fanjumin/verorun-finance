# Site Domains Plugin Routes
# ============================================================
# 重要背景（Phase 5 修正）：
#   admin 应用（:8084）已通过 auth-center 的 admin_bp 提供了一套完整的
#   /admin/api/domains CRUD（见 auth-center/routes/admin.py），前端实际调用的
#   就是它。因此本插件【不再重复注册 CRUD 路由】，避免与 admin_bp 路径冲突。
#
#   本插件当前只提供 Caddy On-Demand TLS 校验端点（/internal/caddy/check），
#   这是新增能力，不与任何现有路由冲突。
#
# 插件路由机制约定（对齐 im_gateway/social_push/oauth_config）：
#   Blueprint 必须自带 url_prefix，route 用相对路径；否则会被 PluginManager
#   强制挂到 /plugin/<id> 前缀下（见 plugin_manager/manager.py:_get_route_prefix）。
import os
import re
import sys

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request

# Caddy 校验端点蓝图：url_prefix=/internal/caddy，route 相对路径
caddy_check_bp = Blueprint("site_domains_caddy", __name__, url_prefix='/internal/caddy')


def _get_main_db():
    """读取主库连接（site_domains 留主库，中间件每请求查询）"""
    from models import get_db
    return get_db()


def _is_loopback_request():
    # 审计 M10：反代后 remote_addr 恒为 127.0.0.1，必须读 nginx 强制覆盖的
    # X-Real-IP（客户端不可伪造）判断真实来源，否则回环防线被外部请求架空。
    ra = request.headers.get('X-Real-IP', '') or request.remote_addr or ''
    return ra in ('127.0.0.1', '::1', 'localhost')


@caddy_check_bp.route('/check', methods=['GET'])
def caddy_check_domain():
    """Caddy On-Demand TLS ask 端点：校验域名是否允许签发证书。

    Caddy 在为某域名签发证书前调用此接口；返回 200 才放行。
    无需 JWT（Caddy 无法携带 token），两道防线保证安全：
      1) 仅信任本机回环（127.0.0.1）请求 —— Caddy 与后端同机
      2) 域名必须已登记在 site_domains 且 is_published=1 —— 签发权绑定业务数据
    防止攻击者用随机域名耗尽 Let's Encrypt 速率限制。
    """
    if not _is_loopback_request():
        return ('forbidden', 403)
    domain = (request.args.get('domain') or '').strip().lower()
    if not domain:
        return ('missing domain', 400)
    if not re.match(r'^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$', domain) or len(domain) > 253:
        return ('invalid domain', 400)
    try:
        with _get_main_db() as conn:
            row = conn.execute(
                'SELECT id FROM site_domains WHERE full_domain=? AND is_published=1',
                (domain,)
            ).fetchone()
    except Exception:
        return ('db error', 500)
    if row:
        return ('ok', 200)
    return ('not allowed', 403)
