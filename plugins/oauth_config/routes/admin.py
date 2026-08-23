#!/usr/bin/env python3
"""OAuth Login Config Plugin — /admin/oauth/configs 配置管理路由

oauth_providers 表收敛至主库 public schema（§12.10），通过 models.get_db() 读写。
"""

from i18n import _
import sys, os
from datetime import datetime

from flask import Blueprint, request, jsonify

oauth_cfg_bp = Blueprint('oauth_config', __name__, url_prefix='/admin/oauth')

VALID_PROVIDERS = ['douyin', 'wechat', 'alipay', 'google', 'telegram']
OAUTH_CALLBACK_PATHS = {
    'douyin': '/auth/douyin/callback',
    'wechat': '/auth/wechat/callback',
    'alipay': '/auth/alipay/callback',
    'google': '/auth/oauth/google/callback',
    'telegram': '/auth/oauth/telegram/callback',
}


def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


def _get_oauth_db():
    """主库连接（oauth_providers 表，§12.10）"""
    from plugins.oauth_config.models import get_db
    return get_db()


@oauth_cfg_bp.route('/configs', methods=['GET'])
def admin_oauth_configs():
    """列出所有站点的 OAuth 配置，支持按 provider 过滤"""
    a, e = _require_admin()
    if e:
        return e
    provider = request.args.get('provider', 'all')
    with _get_oauth_db() as conn:
        if provider == 'all':
            rows = conn.execute(
                'SELECT id, site_domain, provider, client_key, client_secret, is_active, created_at, updated_at '
                'FROM oauth_providers ORDER BY site_domain, provider'
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, site_domain, provider, client_key, client_secret, is_active, created_at, updated_at '
                'FROM oauth_providers WHERE provider=%s ORDER BY site_domain',
                (provider,)
            ).fetchall()
    data = []
    for r in rows:
        secret = r['client_secret'] or ''
        masked_secret = '***' + secret[-4:] if len(secret) > 4 else '***'
        data.append({
            'id': r['id'],
            'site_domain': r['site_domain'],
            'provider': r['provider'],
            'client_key': r['client_key'],
            'client_secret_masked': masked_secret,
            'has_secret': bool(secret),
            'is_active': r['is_active'],
            'created_at': r['created_at'],
            'updated_at': r['updated_at'],
        })
    return jsonify({'success': True, 'data': data})


@oauth_cfg_bp.route('/configs', methods=['POST'])
def admin_oauth_save():
    """保存/更新站点 OAuth 配置（直接写主库 oauth_providers）"""
    a, e = _require_admin()
    if e:
        return e
    d = request.get_json() or {}
    domain = d.get('site_domain', '').strip()
    provider = d.get('provider', 'douyin').strip()
    key = d.get('client_key', '').strip()
    secret = d.get('client_secret', '').strip()

    if provider not in VALID_PROVIDERS:
        return jsonify({'success': False, 'error': f'Unsupported provider: {provider}'}), 400
    if not domain or not key:
        return jsonify({'success': False, 'error': _('Domain and Client Key cannot be empty')}), 400

    # 检查该站点已启用的第三方登录数量（最多 2 个）
    with _get_oauth_db() as conn:
        existing_for_domain = conn.execute(
            'SELECT provider FROM oauth_providers WHERE site_domain=%s AND is_active=1',
            (domain,)
        ).fetchall()
        existing_providers = [r['provider'] for r in existing_for_domain]
        if provider not in existing_providers and len(existing_providers) >= 2:
            return jsonify({
                'success': False,
                'error': _('该站点已启用 {} 个第三方登录（{}），最多允许 2 个。请先禁用或删除一个再添加。').format(len(existing_providers), ", ".join(existing_providers))
            }), 400

    # secret 为空时保留原有（编辑不改密钥场景）
    with _get_oauth_db() as conn:
        if not secret:
            existing = conn.execute(
                'SELECT client_secret FROM oauth_providers WHERE site_domain=%s AND provider=%s',
                (domain, provider)
            ).fetchone()
            if existing and existing['client_secret']:
                secret = existing['client_secret']
            else:
                return jsonify({'success': False, 'error': _('Client Secret cannot be empty (first configuration)')}), 400

        now = datetime.now().isoformat()
        row = conn.execute(
            'SELECT id FROM oauth_providers WHERE site_domain=%s AND provider=%s',
            (domain, provider)
        ).fetchone()
        if row:
            conn.execute(
                'UPDATE oauth_providers SET client_key=%s, client_secret=%s, is_active=1, updated_at=%s WHERE id=%s',
                (key, secret, now, row['id'])
            )
        else:
            conn.execute(
                'INSERT INTO oauth_providers (site_domain, provider, client_key, client_secret, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (domain, provider, key, secret, now, now)
            )
        conn.commit()

    callback_path = OAUTH_CALLBACK_PATHS.get(provider, '/auth/douyin/callback')
    callback = f'https://{domain}{callback_path}'
    _log(a['user_id'], 'save_oauth', 'oauth', domain, f'{provider} for {domain}')
    return jsonify({'success': True, 'data': {'callback_url': callback, 'provider': provider}})


@oauth_cfg_bp.route('/configs/<int:cfg_id>', methods=['DELETE'])
def admin_oauth_delete(cfg_id):
    """删除站点 OAuth 配置"""
    a, e = _require_admin()
    if e:
        return e
    with _get_oauth_db() as conn:
        conn.execute('DELETE FROM oauth_providers WHERE id=%s', (cfg_id,))
        conn.commit()
    _log(a['user_id'], 'delete_oauth', 'oauth', str(cfg_id), 'oauth config deleted')
    return jsonify({'success': True})
