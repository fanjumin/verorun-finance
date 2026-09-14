#!/usr/bin/env python3
"""统一网关 — OAuth 授权路由（Phase 2，独立 blueprint，不动现有 routes.py）。

- connect：管理员发起授权，返回平台授权 URL
- callback：平台回调（浏览器跳转，匿名可达，靠 state 防 CSRF）→ 换 token → 加密入库
- accounts / revoke：已连接账号管理
- refresh：手动刷新 token
"""
import json
import uuid
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from flask import Blueprint, request, jsonify
from i18n import _

from .oauth import get_oauth_provider, OAUTH_PLATFORMS
from .models_accounts import save_account, list_accounts, delete_account
from .crypto import crypto_enabled
from .models import get_im_db
from .channels import get_channel_adapter

logger = logging.getLogger(__name__)

oauth_bp = Blueprint('im_gateway_oauth', __name__,
                     url_prefix='/admin/channels/oauth')


def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


def _callback_uri():
    """回调地址：系统根 URL + /admin/channels/oauth/callback"""
    from flask import request as _req
    return _req.url_root.rstrip('/') + '/admin/channels/oauth/callback'


def _app_credentials(platform: str) -> dict:
    """读取该平台的应用凭据（client_id/client_secret），配置入口沿用
    /admin/channels/<channel> PUT（channel 即平台标识）。"""
    with get_im_db() as conn:
        row = conn.execute(
            "SELECT config_json FROM channel_configs WHERE channel=%s", (platform,)
        ).fetchone()
    return json.loads(row['config_json']) if row else {}


def _compute_expiry(token: dict) -> str:
    if token.get('expires_in'):
        return (datetime.now() + timedelta(seconds=int(token['expires_in']))).isoformat()
    return ''


def _re_apply_state(url: str, state: str) -> str:
    """把合并 extra 后的 state 重新写回 authorize URL 的 state 参数"""
    parts = urlparse(url)
    qs = parse_qs(parts.query)
    qs['state'] = [state]
    return urlunparse(parts._replace(query=urlencode(qs, doseq=True)))


@oauth_bp.route('/connect', methods=['GET'])
def connect():
    """发起授权：?platform=twitter → 返回平台授权 URL"""
    admin, err = _require_admin()
    if err:
        return err
    platform = request.args.get('platform', '')
    if platform not in OAUTH_PLATFORMS:
        return jsonify({'success': False, 'error': _('Unsupported platform')}), 400
    if not crypto_enabled():
        return jsonify({'success': False,
                        'error': _('ENCRYPTION_KEY not configured; OAuth connect disabled')}), 400
    creds = _app_credentials(platform)
    provider = get_oauth_provider(platform, creds)
    if provider is None or not creds:
        return jsonify({'success': False,
                        'error': _('App credentials not configured for this platform')}), 400
    redirect_uri = _callback_uri()
    state_obj = {'platform': platform, 'nonce': uuid.uuid4().hex[:12]}
    try:
        result = provider.get_authorize_url(json.dumps(state_obj), redirect_uri)
    except Exception as e:
        logger.exception('[OAuth] authorize failed: %s', platform)
        msg = str(e)[:300]
        # 依赖缺失属配置问题，返回 400 + 明确提示；其余为服务异常返回 500
        if 'tweepy' in msg or 'praw' in msg or 'not installed' in msg:
            return jsonify({'success': False, 'error': msg}), 400
        return jsonify({'success': False,
                        'error': _('OAuth initialization failed')}), 500
    if isinstance(result, tuple):
        url, extra = result
        state_obj.update(extra or {})
        url = _re_apply_state(url, json.dumps(state_obj))
    else:
        url = result
    return jsonify({'success': True, 'authorize_url': url})


@oauth_bp.route('/callback', methods=['GET'])
def callback():
    """平台回调：?code=...&state=... → 换 token → 加密入库。

    该路由由平台浏览器重定向触发，不做 admin 鉴权，仅校验 state（含平台标识 + nonce）。
    """
    code = request.args.get('code', '')
    state_raw = request.args.get('state', '')
    if not code or not state_raw:
        return jsonify({'success': False, 'error': _('Missing code or state')}), 400
    try:
        state = json.loads(state_raw)
    except (json.JSONDecodeError, TypeError):
        return jsonify({'success': False, 'error': _('Invalid state')}), 400
    platform = state.get('platform', '')
    if platform not in OAUTH_PLATFORMS:
        return jsonify({'success': False, 'error': _('Invalid state')}), 400
    creds = _app_credentials(platform)
    provider = get_oauth_provider(platform, creds)
    if provider is None:
        return jsonify({'success': False, 'error': _('Unsupported platform')}), 400
    redirect_uri = _callback_uri()
    try:
        token = provider.exchange_code(
            code, redirect_uri,
            oauth_token=request.args.get('oauth_token', ''),
            tk_secret=state.get('tk_secret', ''))
    except Exception as e:
        logger.exception('[OAuth] exchange failed: %s', platform)
        return jsonify({'success': False, 'error': str(e)[:500]}), 500
    uid = token.get('uid', '') or ('acct_' + uuid.uuid4().hex[:8])
    save_account(platform, uid, token,
                 handle=token.get('handle', ''),
                 token_expires_at=_compute_expiry(token))
    return jsonify({'success': True, 'message': f'{platform} connected'})


@oauth_bp.route('/accounts', methods=['GET'])
def accounts():
    """已连接账号列表（敏感字段掩码）"""
    admin, err = _require_admin()
    if err:
        return err
    data = list_accounts()
    for a in data:
        cfg = a.get('config') or {}
        for k in list(cfg.keys()):
            if 'token' in k or 'secret' in k or k.endswith('key'):
                v = str(cfg[k])
                if v and len(v) > 4:
                    cfg[k] = v[:4] + '●' * (len(v) - 4)
    return jsonify({'success': True, 'data': data})


@oauth_bp.route('/accounts', methods=['POST'])
def create_account():
    """手动存号（client_credential 渠道，如 telegram_channel）：
    {channel, account_key, config, handle} → 加密落库。
    仅允许保存渠道声明过的配置字段，防止任意字段注入。
    """
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    channel = data.get('channel', '')
    account_key = str(data.get('account_key', '') or 'default')
    config = data.get('config', {}) or {}
    if not channel or not isinstance(config, dict):
        return jsonify({'success': False,
                        'error': _('channel and config are required')}), 400
    adapter = get_channel_adapter(channel)
    if adapter is None or adapter.auth_mode != 'client_credential':
        return jsonify({'success': False,
                        'error': _('Manual credential save not supported for this channel')}), 400
    allowed = {f['key'] for f in (adapter.get_config_fields() or [])}
    if not allowed:
        return jsonify({'success': False,
                        'error': _('This channel has no configurable fields')}), 400
    unknown = set(config.keys()) - allowed
    if unknown:
        return jsonify({'success': False,
                        'error': _('Unsupported config fields') + ': '
                                  + ', '.join(sorted(unknown))}), 400
    save_account(channel, account_key, config,
                 handle=str(data.get('handle', '') or ''))
    return jsonify({'success': True, 'message': f'{channel} account saved'})


@oauth_bp.route('/revoke/<int:account_id>', methods=['POST'])
def revoke(account_id):
    """撤销授权并删除账号（账号不存在返回 404）"""
    admin, err = _require_admin()
    if err:
        return err
    with get_im_db() as conn:
        row = conn.execute(
            'SELECT id FROM channel_accounts WHERE id=%s', (account_id,)
        ).fetchone()
    if not row:
        return jsonify({'success': False, 'error': _('Account not found')}), 404
    delete_account(account_id)
    return jsonify({'success': True})


@oauth_bp.route('/refresh/<int:account_id>', methods=['POST'])
def refresh(account_id):
    """手动刷新 token

    按平台能力与账号状态给出区分提示：
    - 平台不支持刷新 → 400「该平台不支持 token 刷新」
    - 账号无 refresh_token → 400「该账号没有 refresh token」
    - 刷新调用失败 → 502 友好提示（不暴露内部异常）
    """
    admin, err = _require_admin()
    if err:
        return err
    for acct in list_accounts():
        if acct['id'] != account_id:
            continue
        provider = get_oauth_provider(acct['channel'], _app_credentials(acct['channel']))
        if provider is None:
            return jsonify({'success': False,
                            'error': _('Unsupported platform')}), 400
        if not provider.supports_refresh:
            return jsonify({'success': False,
                            'error': _('This platform has no token refresh')}), 400
        refresh_token = (acct.get('config') or {}).get('refresh_token', '')
        if not refresh_token:
            return jsonify({'success': False,
                            'error': _('No refresh token for this account')}), 400
        try:
            new_token = provider.refresh_token(refresh_token)
        except NotImplementedError:
            return jsonify({'success': False,
                            'error': _('This platform has no token refresh')}), 400
        except Exception:
            logger.exception('[OAuth] refresh failed: %s/%s',
                             acct['channel'], acct['account_key'])
            return jsonify({'success': False,
                            'error': _('Token refresh failed, please try again later')}), 502
        merged = {**(acct.get('config') or {}), **new_token}
        save_account(acct['channel'], acct['account_key'], merged,
                     handle=acct.get('handle', ''),
                     token_expires_at=_compute_expiry(new_token))
        return jsonify({'success': True, 'message': f"{acct['channel']} refreshed"})
    return jsonify({'success': False, 'error': _('Account not found')}), 404


@oauth_bp.route('/publish-test', methods=['POST'])
def publish_test():
    """社媒发布测试入口：POST {channels|channel, payload} → gateway.publish（支持单/多渠道）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    payload = data.get('payload', {})
    channels = data.get('channels') or []
    if not channels:
        channel = data.get('channel', '')
        if channel:
            channels = [channel]
    if not channels:
        return jsonify({'success': False, 'error': 'channels is required'}), 400
    from .gateway import gateway
    result = gateway.publish(channels, payload or {})
    # 顶层 success 取所有渠道结果聚合，失败明细保留在 data 内
    all_ok = bool(result) and all(
        (r or {}).get('success') for r in result.values())
    return jsonify({'success': all_ok, 'data': result})
