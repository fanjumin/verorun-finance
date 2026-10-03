#!/usr/bin/env python3
"""IM Gateway — QQ 适配器

批次 D2-a：由「参数校验占位」改为 **QQ 官方机器人 API** 真实实现。
  - 取 token：POST https://bots.qq.com/app/getAppAccessToken (appId + clientSecret)
  - 发消息：POST https://api.sgroup.qq.com/v2/{users|groups}/{target}/messages
    头部 Authorization: QQBot <token>，X-Union-Appid: <appId>

约束（如实）：主动消息（无 msg_id）需 QQ 平台授权；被动回复需在收到消息后携带
msg_id（可由入站事件透传 payload['msg_id']）。未授权时平台会返回错误，按原样透出。
旧字段 app_key/app_secret 不再读取（保留在 DB 行中，不破坏已存配置）。
"""
from i18n import _

from .base import BaseIMAdapter
from .. import http_client
import json as _json

_TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
_API_BASE = 'https://api.sgroup.qq.com'

_token_cache = http_client.TTLCache()


class QQAdapter(BaseIMAdapter):
    channel = 'qq'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'app_id', 'label': 'App ID', 'type': 'text'},
            {'key': 'client_secret', 'label': 'Client Secret', 'type': 'password'},
            {'key': 'bot_secret', 'label': 'Bot Secret (Ed25519 webhook verify)', 'type': 'password'},
            {'key': 'target_type', 'label': 'Target Type (user/group)', 'type': 'text'},
            {'key': 'default_target', 'label': 'Default OpenID / Group OpenID', 'type': 'text'},
        ]

    # ── token（进程内 TTL 缓存） ──

    def _get_token(self, app_id, client_secret):
        cache_key = ('qq', app_id)
        cached = _token_cache.get(cache_key)
        if cached:
            return cached
        _, rd = http_client.request_json(
            'POST', _TOKEN_URL,
            json={'appId': app_id, 'clientSecret': client_secret}
        )
        token = rd.get('access_token', '')
        if not token:
            raise Exception(_('QQ token acquisition failed: {}').format(
                rd.get('message') or rd.get('msg') or rd.get('code')))
        try:
            ttl = int(float(rd.get('expires_in', 7200))) - 300
        except (TypeError, ValueError):
            ttl = 6900
        _token_cache.set(cache_key, token, ttl if ttl > 60 else 6900)
        return token

    def test_connection(self, data):
        app_id = (data.get('app_id') or '').strip()
        client_secret = (data.get('client_secret') or '').strip()
        if not app_id or not client_secret:
            return False, _('QQ app_id and client_secret cannot be empty')
        try:
            _, rd = http_client.request_json(
                'POST', _TOKEN_URL,
                json={'appId': app_id, 'clientSecret': client_secret}
            )
            if rd.get('access_token'):
                return True, _('QQ connection successful!')
            return False, _('QQ returned: {} (code={})').format(
                rd.get('message') or rd.get('msg', 'unknown'), rd.get('code'))
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

    # ── 消息发送 ──

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='qq' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("QQ channel is not configured"))
        return _json.loads(row['config_json'])

    def send(self, payload: dict, **kw) -> dict:
        content = payload.get('content') or payload.get('text') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        try:
            cfg = self._get_config()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        app_id = (cfg.get('app_id') or '').strip()
        client_secret = (cfg.get('client_secret') or '').strip()
        if not app_id or not client_secret:
            return {'success': False, 'error': _('QQ app_id and client_secret cannot be empty')}

        target_type = (cfg.get('target_type') or 'group').strip().lower()
        if target_type not in ('user', 'group'):
            return {'success': False, 'error': _('QQ target_type must be user or group')}
        target = str(payload.get('to') or cfg.get('default_target') or '').strip()
        if not target:
            return {'success': False, 'error': _('QQ recipient (openid or group_openid) is not configured')}

        path = '/v2/users/{}/messages'.format(target) if target_type == 'user' \
            else '/v2/groups/{}/messages'.format(target)
        body = {'content': str(content)[:2000], 'msg_type': 0}
        # 被动回复：携带入站 msg_id（须在平台时限内）
        msg_id = payload.get('msg_id') or payload.get('message_id')
        if msg_id:
            body['msg_id'] = msg_id
            body['msg_seq'] = int(payload.get('msg_seq') or 1)

        try:
            token = self._get_token(app_id, client_secret)
            status, rd = http_client.request_json(
                'POST', _API_BASE + path,
                json=body,
                headers={'Authorization': 'QQBot ' + token, 'X-Union-Appid': app_id}
            )
            if status >= 400:
                return {'success': False, 'error': rd.get('message') or rd.get('msg') or str(rd)[:500]}
            return {'success': True, 'message_id': rd.get('id'), 'timestamp': rd.get('timestamp')}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}