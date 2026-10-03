#!/usr/bin/env python3
"""IM Gateway — Slack 适配器（批次 D2-b 新增）

出站：chat.postMessage / 连接测试：auth.test，均走 http_client（统一超时）。
入站请求签名（v0 HMAC-SHA256）由 webhook_signing 统一实现（D3-a）。
"""
from i18n import _

from .base import BaseIMAdapter
from .. import http_client

_API = 'https://slack.com/api'


class SlackAdapter(BaseIMAdapter):
    channel = 'slack'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'bot_token', 'label': 'Bot Token (xoxb-...)', 'type': 'password'},
            {'key': 'default_channel', 'label': 'Default Channel ID', 'type': 'text'},
            {'key': 'signing_secret', 'label': 'Signing Secret', 'type': 'password'},
        ]

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='slack' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("Slack channel is not configured"))
        import json as _json
        return _json.loads(row['config_json'])

    @staticmethod
    def _auth_header(token):
        return {'Authorization': 'Bearer ' + token}

    def test_connection(self, data):
        bot_token = (data.get('bot_token') or '').strip()
        if not bot_token:
            return False, _('Slack bot token is required')
        try:
            _, rd = http_client.request_json(
                'POST', _API + '/auth.test',
                headers=self._auth_header(bot_token)
            )
            if rd.get('ok'):
                return True, _('Slack connection successful!')
            return False, _('Slack returned: {}').format(rd.get('error', 'unknown'))
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
            return {'success': False, 'error': _('Slack bot token is required')}
        channel = str(payload.get('to') or cfg.get('default_channel') or '').strip()
        if not channel:
            return {'success': False, 'error': _('Slack channel (or default_channel) is not configured')}
        try:
            _, rd = http_client.request_json(
                'POST', _API + '/chat.postMessage',
                json={'channel': channel, 'text': str(content)[:4000]},
                headers=self._auth_header(bot_token)
            )
            if not rd.get('ok'):
                return {'success': False, 'error': rd.get('error', _('Slack send failed'))}
            return {'success': True, 'ts': rd.get('ts'), 'channel': rd.get('channel')}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}