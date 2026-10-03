#!/usr/bin/env python3
"""IM Gateway — Discord 适配器（批次 D2-b 新增）

出站：POST /channels/{id}/messages；连接测试：GET /users/@me；均走 http_client。
入站交互（Ed25519 验签 + PING/PONG）由 webhook_signing / routes_webhook 统一处理（D3-a）。
"""
from i18n import _

from .base import BaseIMAdapter
from .. import http_client

_API = 'https://discord.com/api/v10'


class DiscordAdapter(BaseIMAdapter):
    channel = 'discord'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'bot_token', 'label': 'Bot Token', 'type': 'password'},
            {'key': 'default_channel_id', 'label': 'Default Channel ID', 'type': 'text'},
            {'key': 'application_public_key', 'label': 'Application Public Key (hex)', 'type': 'text'},
        ]

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='discord' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("Discord channel is not configured"))
        import json as _json
        return _json.loads(row['config_json'])

    @staticmethod
    def _auth_header(token):
        return {'Authorization': 'Bot ' + token}

    def test_connection(self, data):
        bot_token = (data.get('bot_token') or '').strip()
        if not bot_token:
            return False, _('Discord bot token is required')
        try:
            status, rd = http_client.request_json(
                'GET', _API + '/users/@me',
                headers=self._auth_header(bot_token)
            )
            if status == 200 and rd.get('id'):
                return True, _('Discord connection successful!')
            return False, _('Discord returned HTTP {}').format(status)
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

    def send(self, payload: dict, **kw) -> dict:
        content = payload.get('content') or payload.get('text') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        try:
            cfg = self._get_config()
        except Exception as e:
            return {'success': False, 'error': str(e)}
        bot_token = (cfg.get('bot_token') or '').strip()
        if not bot_token:
            return {'success': False, 'error': _('Discord bot token is required')}
        channel_id = str(payload.get('to') or cfg.get('default_channel_id') or '').strip()
        if not channel_id:
            return {'success': False, 'error': _('Discord channel_id is not configured')}
        try:
            status, rd = http_client.request_json(
                'POST', '{}/channels/{}/messages'.format(_API, channel_id),
                json={'content': str(content)[:2000]},
                headers=self._auth_header(bot_token)
            )
            if status >= 400:
                return {'success': False, 'error': rd.get('message') or _('Discord send failed')}
            return {'success': True, 'message_id': rd.get('id'), 'channel_id': rd.get('channel_id')}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}