#!/usr/bin/env python3
"""Telegram Channel push provider — publish content to a channel via Bot API.

Uses only standard library (urllib), no extra dependencies required.

The bot must be an administrator of the target channel with "Post Messages"
permission. The channel username (e.g. @mychannel) or channel ID is configured
via system_config.
"""
import json
import urllib.request
import urllib.parse
from typing import Optional
from i18n import _
from .base import BaseSocialProvider


API_BASE = 'https://api.telegram.org/bot'


class TelegramChannelPushProvider(BaseSocialProvider):
    """Telegram Bot API — send formatted messages to a channel."""

    PROVIDER = 'telegram'
    DISPLAY_NAME = 'Telegram Channel'
    ICON = '✈'

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}

    @property
    def _bot_token(self) -> str:
        return self._config.get('telegram_bot_token', '')

    @property
    def _channel(self) -> str:
        return self._config.get('telegram_channel', '')

    def is_configured(self) -> bool:
        return bool(self._bot_token and self._channel)

    def get_config_fields(self) -> list:
        return [
            {'key': 'telegram_bot_token', 'label': 'Bot Token', 'type': 'password'},
            {'key': 'telegram_channel',   'label': 'Channel (@username or numeric ID)', 'type': 'text'},
        ]

    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                config: Optional[dict] = None, **kwargs) -> dict:
        if config:
            self._config = config

        if not self.is_configured():
            return {'success': False, 'error': _('Telegram not configured')}

        try:
            # Build message text with optional markdown formatting
            text = self._build_message(title, body, summary, link_url)

            if image_url:
                # 有配图 → sendPhoto（caption 上限 1024 字符）
                api_url = f'{API_BASE}{self._bot_token}/sendPhoto'
                payload = {
                    'chat_id': self._channel,
                    'photo': image_url,
                    'caption': text[:1024],
                    'parse_mode': 'HTML',
                }
            else:
                api_url = f'{API_BASE}{self._bot_token}/sendMessage'
                payload = {
                    'chat_id': self._channel,
                    'text': text,
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': False,
                }

            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                api_url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read().decode())

            if not result.get('ok'):
                desc = result.get('description', _('Unknown error'))
                return {'success': False, 'error': _('Telegram API Error: {desc}').format(desc=desc)}

            message = result.get('result', {})
            message_id = str(message.get('message_id', ''))

            return {
                'success': True,
                'post_id': message_id,
                'url': f'https://t.me/{self._channel.lstrip("@")}/{message_id}'
                       if self._channel.startswith('@') else '',
                'error': '',
            }
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    @staticmethod
    def _build_message(title: str, body: str, summary: str = '',
                       link_url: str = '') -> str:
        """Format the message with HTML tags suitable for Telegram."""
        parts = []
        if title:
            parts.append(f'<b>{_escape(title)}</b>')

        if link_url:
            parts.append(f'<a href="{_escape(link_url)}">{_escape(link_url)}</a>')

        if summary:
            parts.append(_escape(summary))
        elif body:
            parts.append(_escape(body[:1024]))  # Telegram 4096 total limit, leave room

        text = '\n\n'.join(parts) if parts else (_escape(title or body or ''))
        # Telegram sendMessage limit = 4096 characters
        return text[:4092]


def _escape(text: str) -> str:
    """Escape HTML reserved characters for Telegram parse_mode=HTML."""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))
