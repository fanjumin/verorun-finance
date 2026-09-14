#!/usr/bin/env python3
"""IM Gateway — LINE 适配器"""
from i18n import _
import os
import json as _json
import urllib.request as _ur

from .base import BaseIMAdapter


class LINEAdapter(BaseIMAdapter):
    channel = 'line'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'channel_secret', 'label': 'Channel Secret', 'type': 'password'},
            {'key': 'access_token', 'label': 'Channel Access Token', 'type': 'password'},
            {'key': 'webhook_url', 'label': 'Webhook URL', 'type': 'text'},
        ]

    def test_connection(self, data):
        token = (data.get('access_token') or '').strip()
        if not token:
            return False, _('Channel Access Token cannot be empty')
        try:
            req = _ur.Request(
                'https://api.line.me/v2/bot/info',
                headers={'Authorization': f'Bearer {token}'}
            )
            resp = _json.loads(_ur.urlopen(req, timeout=10).read())
            if resp.get('userId'):
                name = resp.get('displayName', '')
                return True, _('LINE Connected! Bot: {}').format(name)
            return False, _('LINE Response: {}').format(resp)
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

    def get_env_fallback(self):
        cfg = {}
        secret = os.environ.get('LINE_CHANNEL_SECRET', '')
        token = os.environ.get('LINE_ACCESS_TOKEN', '')
        webhook = os.environ.get('LINE_WEBHOOK_URL', '')
        if secret:
            cfg['channel_secret'] = self._mask(secret)
        if token:
            cfg['access_token'] = self._mask(token)
        if webhook:
            cfg['webhook_url'] = webhook
        return cfg

    # ── 消息发送 ──

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='line' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("LINE channel is not configured"))
        return _json.loads(row['config_json'])

    def send(self, payload: dict, **kw) -> dict:
        """通过 Messaging API push 发送文本消息（payload['to'] 为 userId）。"""
        content = payload.get('content') or payload.get('text') or ''
        to = payload.get('to') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        if not to:
            return {'success': False, 'error': _('LINE recipient (userId) is required')}
        cfg = self._get_config()
        token = cfg.get('access_token', '')
        if not token:
            return {'success': False, 'error': _('LINE Channel Access Token is empty')}
        body = _json.dumps({'to': to, 'messages': [{'type': 'text', 'text': str(content)[:5000]}]}).encode()
        req = _ur.Request('https://api.line.me/v2/bot/message/push', data=body,
                          headers={'Authorization': 'Bearer ' + token,
                                   'Content-Type': 'application/json'})
        try:
            _ur.urlopen(req, timeout=10).read()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
