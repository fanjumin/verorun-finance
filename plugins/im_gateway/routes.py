#!/usr/bin/env python3
"""IM Gateway Plugin — 管理端频道配置 API

迁移自 auth-center/routes/admin.py 的频道管理 REST 端点。
路由前缀 /admin/channels，复用主系统管理员鉴权与操作日志。
频道配置读写插件独立库 im_gateway.db，第三方逻辑走 adapters。
"""
from i18n import _
import sys
import os
import json

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify

from .models import get_im_db
from .adapters import get_adapter, list_channels

im_bp = Blueprint('im_gateway', __name__, url_prefix='/admin/channels')


def _require_admin():
    """复用主系统的管理员鉴权"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    """复用主系统的操作日志"""
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


def _mask_config(cfg):
    """对 secret 类字段掩码"""
    for key in list(cfg.keys()):
        if 'secret' in key or 'token' in key or 'key' in key:
            val = cfg[key]
            if val and len(val) > 4:
                cfg[key] = val[:4] + '●' * (len(val) - 4)
    return cfg


@im_bp.route('', methods=['GET'])
@im_bp.route('/', methods=['GET'])
def list_channels_route():
    """获取所有频道配置（secret 值掩码）"""
    admin, err = _require_admin()
    if err:
        return err
    with get_im_db() as conn:
        rows = conn.execute(
            'SELECT id, channel, config_json, is_enabled, created_at, updated_at '
            'FROM channel_configs ORDER BY id'
        ).fetchall()
    result = []
    for r in rows:
        cfg = json.loads(r['config_json'] or '{}')
        result.append({
            'id': r['id'],
            'channel': r['channel'],
            'config': _mask_config(cfg),
            'is_enabled': r['is_enabled'],
            'created_at': r['created_at'],
            'updated_at': r['updated_at'],
        })
    return jsonify({'success': True, 'data': result})


@im_bp.route('/<channel>', methods=['GET'])
def get_channel(channel):
    """获取单个频道配置（secret 掩码）"""
    admin, err = _require_admin()
    if err:
        return err
    adapter = get_adapter(channel)
    env_fallback = adapter.get_env_fallback() if adapter else {}
    with get_im_db() as conn:
        row = conn.execute(
            'SELECT id, channel, config_json, is_enabled, created_at, updated_at '
            'FROM channel_configs WHERE channel=%s',
            (channel,)
        ).fetchone()
    if not row:
        return jsonify({'success': True, 'data': {
            'channel': channel,
            'config': {},
            'is_enabled': 0,
            'from_env': env_fallback,
        }})
    cfg = json.loads(row['config_json'] or '{}')
    return jsonify({'success': True, 'data': {
        'channel': row['channel'],
        'config': _mask_config(cfg),
        'is_enabled': row['is_enabled'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'from_env': env_fallback,
    }})


@im_bp.route('/<channel>', methods=['PUT'])
def update_channel(channel):
    """保存/更新频道配置（掩码值不覆盖旧值）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    config = data.get('config', {})
    is_enabled = 1 if data.get('is_enabled', False) else 0

    with get_im_db() as conn:
        existing = conn.execute(
            'SELECT config_json FROM channel_configs WHERE channel=%s', (channel,)
        ).fetchone()
        old_cfg = json.loads(existing['config_json']) if existing else {}

        merged = dict(old_cfg)
        for k, v in config.items():
            if isinstance(v, str) and '●' in v:
                continue  # 掩码值，不覆盖
            merged[k] = v

        conn.execute(
            """INSERT INTO channel_configs (channel, config_json, is_enabled)
               VALUES (%s, %s, %s)
               ON CONFLICT (channel) DO UPDATE SET
                   config_json=EXCLUDED.config_json,
                   is_enabled=EXCLUDED.is_enabled,
                   updated_at=NOW()""",
            (channel, json.dumps(merged, ensure_ascii=False), is_enabled)
        )
        conn.commit()
    _log(admin['user_id'], 'update', 'channel_config', channel, _('Channel configuration updated'))
    return jsonify({'success': True, 'message': f'{channel} configuration has been saved'})


@im_bp.route('/<channel>/test', methods=['POST'])
def test_channel(channel):
    """测试频道连接"""
    admin, err = _require_admin()
    if err:
        return err
    adapter = get_adapter(channel)
    if adapter is None:
        return jsonify({'success': False, 'error': f'{channel} does not support connection test'}), 400
    data = request.get_json(force=True) or {}
    ok, message = adapter.test_connection(data)
    if ok:
        return jsonify({'success': True, 'message': message})
    return jsonify({'success': False, 'error': message}), 400
