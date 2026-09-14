#!/usr/bin/env python3
"""统一网关 — 聚合概览端点（卡片式管理 UI 数据源，Phase 1）。

GET /admin/channels/overview → 五类结构化数据：
    im        即时通讯（adapters 注册表 + channel_configs）
    social    社媒（统一渠道注册表 + channel_accounts，telegram 标记共享来源）
    publish   发布（已连接社媒目标渠道）
    login     第三方登录（login_providers 表，Phase 5 建表；表未建时优雅返回空列表）
    developer 开发者登录（复用 unified_auth_service.list_keys，只读）

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
    """复用主系统管理员鉴权（与 routes.py / routes_oauth.py 一致）"""
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


def _social_section() -> list:
    """社媒：统一渠道注册表 + channel_accounts；telegram_channel 标记 shared_from='im'"""
    from .channels import list_channels
    from .models_accounts import list_accounts
    from .models import get_im_db

    # Telegram 共享判定：channel_configs 中 telegram 已配置 bot_token → 共享可用
    shared = False
    with get_im_db() as conn:
        row = conn.execute(
            "SELECT config_json FROM channel_configs WHERE channel='telegram'"
        ).fetchone()
    if row and json.loads(row['config_json'] or '{}').get('bot_token'):
        shared = True

    result = []
    for meta in list_channels():
        ch = meta['channel']
        accts = list_accounts(ch)
        result.append({
            'channel': ch,
            'channel_type': meta['channel_type'],
            'auth_mode': meta['auth_mode'],
            'connected': any(a.get('is_enabled') for a in accts),
            'shared_from': 'im' if (ch == 'telegram_channel' and shared) else None,
            'accounts': [
                {
                    'id': a['id'],
                    'handle': a.get('handle') or a.get('account_key') or '',
                    'token_expires_at': a.get('token_expires_at') or '',
                    'is_enabled': int(a.get('is_enabled') or 0),
                }
                for a in accts
            ],
        })
    return result


def _publish_section() -> dict:
    """发布：已连接社媒目标渠道列表"""
    from .channels import list_channels
    from .models_accounts import list_accounts
    targets = [
        meta['channel'] for meta in list_channels()
        if any(a.get('is_enabled') for a in list_accounts(meta['channel']))
    ]
    return {'targets': targets, 'connected_count': len(targets)}


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


def _developer_section(user_id: int) -> list:
    """开发者登录：复用 unified_auth_service.list_keys（只读，不暴露 key_hash）"""
    try:
        from services.unified_auth_service import UnifiedAuthService
        keys = UnifiedAuthService().list_keys(user_id)
        return [
            {
                'id': k['id'],
                'name': k.get('name') or '',
                'key_prefix': k.get('key_prefix') or '',
                'key_type': k.get('key_type') or '',
                'expire_at': k.get('expire_at') or '',
                'status': k.get('status') or '',
            }
            for k in keys
        ]
    except Exception:
        logger.debug('[Overview] developer keys read failed; return empty')
        return []


def _miniapp_section() -> list:
    """小程序开发者账户：复用 mini_app_builder dev_accounts 数据层（Phase 7）。

    mini_app_builder 未启用时优雅返回空列表；不重复建表、不改其代码。
    """
    try:
        from plugins.mini_app_builder.submodules.accounts import models as _acc
        rows = _acc.get_all()
    except Exception:
        logger.debug('[Overview] mini_app_builder accounts not ready; return empty')
        return []
    from .routes_miniapp import LOGIN_METHODS
    out = []
    for a in rows:
        meta = LOGIN_METHODS.get(a.get('platform', ''), {})
        out.append({
            'id': a.get('id'),
            'platform': a.get('platform', ''),
            'account_name': a.get('account_name', ''),
            'app_id': a.get('app_id', '') or '',
            'is_active': int(a.get('is_active') or 0),
            'method_type': meta.get('method_type', ''),
            'updated_at': a.get('updated_at', '') or '',
        })
    return out


@overview_bp.route('/overview', methods=['GET'])
def overview():
    """聚合概览：六类结构化数据，供卡片式管理 UI 渲染"""
    admin, err = _require_admin()
    if err:
        return err
    data = {
        'im': _im_section(),
        'social': _social_section(),
        'publish': _publish_section(),
        'login': _login_section(),
        'developer': _developer_section(admin['user_id']),
        'miniapp': _miniapp_section(),
    }
    return jsonify({'success': True, 'data': data})
