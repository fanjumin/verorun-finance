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
                return True, f'LINE Connected! Bot: {name}'
            return False, f"LINE Response: {resp}"
        except Exception as e:
            return False, f'Connection failed: {str(e)}'

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
