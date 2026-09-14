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
                return True, _('Telegram Connected! Bot: {}').format(bot_name)
            return False, _("Telegram returned error: {}").format(resp.get('description', _('Unknown')))
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

    def get_env_fallback(self):
        cfg = {}
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        webhook = os.environ.get('TELEGRAM_WEBHOOK_URL', '')
        if token:
            cfg['bot_token'] = self._mask(token)
        if webhook:
            cfg['webhook_url'] = webhook
        return cfg

    # ── 消息发送 ──

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='telegram' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("Telegram channel is not configured"))
        return _json.loads(row['config_json'])

    def send(self, payload: dict, **kw) -> dict:
        """通过 Bot API sendMessage 发送文本消息（payload['to'] 为 chat_id）。"""
        content = payload.get('content') or payload.get('text') or ''
        to = payload.get('to') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        if not to:
            return {'success': False, 'error': _('Telegram recipient (chat_id) is required')}
        cfg = self._get_config()
        token = cfg.get('bot_token', '')
        if not token:
            return {'success': False, 'error': _('Telegram Bot Token is empty')}
        body = _json.dumps({'chat_id': to, 'text': str(content)[:4000],
                            'disable_web_page_preview': True}).encode()
        try:
            resp = _json.loads(_ur.urlopen(_ur.Request(
                f'https://api.telegram.org/bot{token}/sendMessage',
                data=body, headers={'Content-Type': 'application/json'}
            )).read())
            if resp.get('ok'):
                return {'success': True, 'message_id': (resp.get('result') or {}).get('message_id')}
            return {'success': False, 'error': resp.get('description', _('Telegram send failed'))}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
