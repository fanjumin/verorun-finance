#!/usr/bin/env python3
"""IM Gateway — Telegram 适配器"""
from i18n import _
import os
import json as _json
import urllib.request as _ur

from .base import BaseIMAdapter


class TelegramAdapter(BaseIMAdapter):
    channel = 'telegram'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'bot_token', 'label': 'Bot Token', 'type': 'password'},
            {'key': 'webhook_url', 'label': 'Webhook URL', 'type': 'text'},
            {'key': 'allow_groups', 'label': 'Allow Group Chat (true/false)', 'type': 'text'},
        ]

    def test_connection(self, data):
        token = (data.get('bot_token') or '').strip()
        if not token:
            return False, _('Bot Token cannot be empty')
        try:
            resp = _json.loads(_ur.urlopen(
                _ur.Request(f'https://api.telegram.org/bot{token}/getMe'),
                timeout=10
            ).read())
            if resp.get('ok'):
                bot_name = resp['result'].get('first_name', '')
                return True, f'Telegram Connected! Bot: {bot_name}'
            return False, _("Telegram 返回错误: {}").format(resp.get('description', _('Unknown')))
        except Exception as e:
            return False, f'Connection failed: {str(e)}'

    def get_env_fallback(self):
        cfg = {}
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        webhook = os.environ.get('TELEGRAM_WEBHOOK_URL', '')
        if token:
            cfg['bot_token'] = self._mask(token)
        if webhook:
            cfg['webhook_url'] = webhook
        return cfg
