#!/usr/bin/env python3
"""统一网关 — 聚合概览端点（卡片式管理 UI 数据源）。

GET /admin/channels/overview → 两类结构化数据：
    im        即时通讯（adapters 注册表 + channel_configs）
    login     第三方登录（login_providers 表；表未建时优雅返回空列表）

社媒(social) / 发布(publish) 两段已随职责收敛迁至 social_push 插件；
开发者 API Key / 小程序开发者账户迁至 mini_app_builder 自有端点。
纯读聚合：不新建表、不改既有路由、不写任何凭据。
"""
import json
import logging
import os
import sys

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, jsonify  # noqa: E402

logger = logging.getLogger(__name__)

overview_bp = Blueprint('im_gateway_overview', __name__,
                        url_prefix='/admin/channels')


def _require_admin():
    """复用主系统管理员鉴权（与 routes.py / routes_login.py 一致）"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _mask(cfg):
    """secret 类字段掩码（与 routes.py _mask_config 同规则）"""
    for key in list(cfg.keys()):
        if 'secret' in key or 'token' in key or 'key' in key:
            val = cfg[key]
            if val and len(val) > 4:
                cfg[key] = val[:4] + '●' * (len(val) - 4)
    return cfg


def _im_section() -> list:
    """即时通讯：adapters 注册表 + channel_configs（掩码配置 / env 兜底 / 字段声明）"""
    from .adapters import list_channels as im_list, get_adapter
    from .models import get_im_db
    with get_im_db() as conn:
        rows = conn.execute(
            'SELECT channel, config_json, is_enabled FROM channel_configs'
        ).fetchall()
    configs = {r['channel']: r for r in rows}
    result = []
    for ch in im_list():
        adapter = get_adapter(ch)
        row = configs.get(ch)
        cfg = json.loads(row['config_json']) if row else {}
        result.append({
            'channel': ch,
            'enabled': int(row['is_enabled']) if row else 0,
            'config': _mask(dict(cfg)),
            'from_env': adapter.get_env_fallback() if adapter else {},
            'config_fields': adapter.get_config_fields() if adapter else [],
        })
    return result


def _mask_secret(secret: str) -> str:
    """secret 单值掩码（与 routes_login._mask_secret 同规则）"""
    if not secret:
        return ''
    if len(secret) <= 4:
        return '●' * len(secret)
    return secret[:4] + '●' * (len(secret) - 4)


def _login_section() -> list:
    """第三方登录：内置提供方目录 + login_providers 表（Phase 5 建表）。

    未保存过的内置提供方也一并返回（configured=False），便于前端渲染
    「未配置」卡片并直接进入配置表单。表不存在时优雅返回空列表。
    """
    try:
        from .login import list_login_providers
        from .models import get_im_db
        with get_im_db() as conn:
            rows = conn.execute(
                'SELECT provider, display_name, client_id, client_secret, scopes, '
                'redirect_uri, is_enabled FROM login_providers ORDER BY id'
            ).fetchall()
        configs = {r['provider']: r for r in rows}
        result = []
        for p in list_login_providers():
            cfg = configs.get(p['id'], {})
            result.append({
                'provider': p['id'],
                'display_name': cfg.get('display_name') or p['display_name'],
                'configured': bool(cfg.get('client_id')),
                'is_enabled': int(cfg.get('is_enabled') or 0),
                'client_id': cfg.get('client_id', '') or '',
                'client_secret_masked': _mask_secret(cfg.get('client_secret', '') or ''),
                'scopes': cfg.get('scopes', '') or '',
                'redirect_uri': cfg.get('redirect_uri', '') or '',
                'default_scopes': p['default_scopes'],
            })
        return result
    except Exception:
        logger.debug('[Overview] login_providers not ready; return empty')
        return []


@overview_bp.route('/overview', methods=['GET'])
def overview():
    """聚合概览：im / login 两类结构化数据，供卡片式管理 UI 渲染。

    注：social / publish 两段已随职责收敛迁至 social_push；
    developer（API Key）与 miniapp（开发者账户）迁至 mini_app_builder。
    """
    admin, err = _require_admin()
    if err:
        return err
    data = {
        'im': _im_section(),
        'login': _login_section(),
    }
    return jsonify({'success': True, 'data': data})
