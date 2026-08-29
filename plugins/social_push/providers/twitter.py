#!/usr/bin/env python3
"""X/Twitter push provider — tweet publishing via tweepy.

Credentials are passed via a config dict (from system_config) rather than
environment variables, to keep consistency with other social_push providers.
"""
from typing import Optional
from i18n import _
from .base import BaseSocialProvider


class TwitterPushProvider(BaseSocialProvider):
    """Twitter/X API v2 — tweet publishing using tweepy."""

    PROVIDER = 'twitter'
    DISPLAY_NAME = 'X (Twitter)'
    ICON = '𝕏'

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}

    @property
    def _api_key(self) -> str:
        return self._config.get('twitter_api_key', '')

    @property
    def _api_secret(self) -> str:
        return self._config.get('twitter_api_secret', '')

    @property
    def _access_token(self) -> str:
        return self._config.get('twitter_access_token', '')

    @property
    def _access_secret(self) -> str:
        return self._config.get('twitter_access_secret', '')

    @property
    def _bearer_token(self) -> str:
        return self._config.get('twitter_bearer_token', '')

    def is_configured(self) -> bool:
        return bool(self._bearer_token or (self._api_key and self._api_secret))

    def get_config_fields(self) -> list:
        return [
            {'key': 'twitter_api_key', 'label': 'API Key', 'type': 'text'},
            {'key': 'twitter_api_secret', 'label': 'API Secret', 'type': 'password'},
            {'key': 'twitter_access_token', 'label': 'Access Token', 'type': 'password'},
            {'key': 'twitter_access_secret', 'label': 'Access Secret', 'type': 'password'},
            {'key': 'twitter_bearer_token', 'label': 'Bearer Token', 'type': 'password'},
        ]

    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                config: Optional[dict] = None, **kwargs) -> dict:
        if config:
            self._config = config

        if not self.is_configured():
            return {'success': False, 'error': _('Twitter/X not configured')}

        try:
            import tweepy

            client = tweepy.Client(
                bearer_token=self._bearer_token,
                consumer_key=self._api_key,
                consumer_secret=self._api_secret,
                access_token=self._access_token,
                access_token_secret=self._access_secret,
            )

            # Build tweet text within 280-character limit
            text = (title or '')[:280]
            if link_url:
                remaining = 280 - len(link_url) - 2
                text = (title or '')[:remaining] + ' ' + link_url

            resp = client.create_tweet(text=text)
            tweet_id = str(resp.data.id) if resp.data else ''

            return {
                'success': True,
                'post_id': tweet_id,
                'url': f'https://twitter.com/i/web/status/{tweet_id}' if tweet_id else '',
                'error': '',
            }
        except ImportError:
            return {'success': False, 'error': _('tweepy not installed')}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}
