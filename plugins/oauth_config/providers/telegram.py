#!/usr/bin/env python3
"""Telegram OAuth Provider — Telegram Login Widget.

Uses Telegram's OAuth-like flow:
  1. Redirect user to Telegram auth page
  2. Telegram redirects back with user data + hash
  3. Backend verifies hash via HMAC-SHA256 (bot token as secret)

Environment variables:
    TELEGRAM_BOT_TOKEN  (used as fallback if not configured via oauth_providers DB)

The bot token is used as client_secret for hash verification.
The bot username is registered as client_key in oauth_providers DB.
"""
import os, hashlib, hmac, urllib.parse, urllib.request, json
from typing import Optional

from plugins.oauth_config.providers.base import BaseOAuthProvider


class TelegramOAuthProvider(BaseOAuthProvider):
    """Telegram Login Widget OAuth provider."""

    PROVIDER = 'telegram'

    AUTHORIZE_URL = 'https://oauth.telegram.org/auth'
    TOKEN_URL = ''
    USERINFO_URL = ''

    def _db_config(self, key):
        """从 oauth_providers 表读取 telegram 配置。"""
        try:
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    'SELECT client_key, client_secret FROM oauth_providers '
                    'WHERE provider=%s AND is_active=1 LIMIT 1',
                    ('telegram',)
                ).fetchone()
            if row:
                return row[key]
        except Exception:
            pass
        return ''

    def get_client_id(self) -> str:
        """Return the Telegram bot username (client_key in DB)."""
        db_id = self._db_config('client_key')
        return db_id or os.environ.get('TELEGRAM_BOT_USERNAME', '')

    def get_client_secret(self) -> str:
        """Return the Telegram bot token (client_secret in DB)."""
        db_secret = self._db_config('client_secret')
        return db_secret or os.environ.get('TELEGRAM_BOT_TOKEN', '')

    def is_configured(self) -> bool:
        return bool(self.get_client_id() and self.get_client_secret())

    def get_authorize_url(self, redirect_uri: str, state: str = 'login') -> str:
        """Generate Telegram OAuth authorize URL.

        Telegram requires bot_id (numeric) for the auth URL.
        We derive bot_id from the bot token (token format: {bot_id}:{hash}).
        """
        token = self.get_client_secret()
        bot_id = token.split(':')[0] if ':' in token else ''
        if not bot_id:
            return ''

        params = urllib.parse.urlencode({
            'bot_id': bot_id,
            'origin': redirect_uri,
            'return_to': redirect_uri,
            'embed': '0',
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Telegram does not use code-based exchange.
        Verification is done via hash validation in get_user_by_code().
        """
        return {'error': 'Telegram uses hash verification, not code exchange. Use get_user_by_code() directly.'}

    def get_userinfo(self, access_token: str) -> dict:
        """Not used for Telegram. User data is returned directly in callback."""
        return {'error': 'Telegram does not support token-based userinfo.'}

    def get_user_by_code(self, code: str, redirect_uri: str) -> dict:
        """Telegram callback hash verification.

        The 'code' parameter here is actually the full query string from Telegram
        callback, e.g. 'hash=xxx&id=123&first_name=John&auth_date=1234567890'.

        Returns dict with open_id, nickname, avatar or {'error': '...'} on failure.
        """
        data = urllib.parse.parse_qs(code)
        tg_hash = data.get('hash', [None])[0]
        if not tg_hash:
            return {'error': 'telegram: missing hash in callback'}

        # Build data-check-string: sort all keys except hash, join as key=value\n
        items = []
        for k, v in data.items():
            if k == 'hash':
                continue
            items.append((k, v[0] if isinstance(v, list) else v))
        items.sort(key=lambda x: x[0])
        data_check_str = '\n'.join(f'{k}={v}' for k, v in items)

        # Compute secret key: HMAC-SHA256(bot_token, "WebAppData")
        bot_token = self.get_client_secret()
        secret_key = hmac.new(
            bot_token.encode('utf-8'),
            b'WebAppData',
            hashlib.sha256
        ).digest()

        # Compute hash
        computed_hash = hmac.new(
            secret_key,
            data_check_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if computed_hash != tg_hash:
            return {'error': 'telegram: hash verification failed'}

        # Check auth_date is not too old (allow 24h)
        import time
        auth_date = int(data.get('auth_date', [0])[0])
        if time.time() - auth_date > 86400:
            return {'error': 'telegram: auth_date too old'}

        user_id = data.get('id', [None])[0]
        if not user_id:
            return {'error': 'telegram: missing user id'}

        first_name = data.get('first_name', [''])[0]
        last_name = data.get('last_name', [''])[0]
        username = data.get('username', [''])[0]
        photo_url = data.get('photo_url', [None])[0]

        display_name = first_name
        if last_name:
            display_name += f' {last_name}'
        if not display_name and username:
            display_name = username

        return {
            'open_id': str(user_id),
            'nickname': display_name,
            'avatar': photo_url or '',
            'email': '',
            'username': username or '',
        }
