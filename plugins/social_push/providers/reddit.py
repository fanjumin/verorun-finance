#!/usr/bin/env python3
"""Reddit push provider — submit link/text post via PRAW.

Requires: pip install praw

Credentials (from system_config):
    reddit_client_id, reddit_client_secret, reddit_username, reddit_password

Note: Reddit API v1 (OAuth 2.0 + Basic Auth for script apps).
"""
from typing import Optional
from i18n import _
from .base import BaseSocialProvider


class RedditPushProvider(BaseSocialProvider):
    """Reddit API — submit a link or text post to a subreddit."""

    PROVIDER = 'reddit'
    DISPLAY_NAME = 'Reddit'
    ICON = 'rd'

    # Default subreddit if not specified; falls back to target_audience field
    DEFAULT_SUBREDDIT = ''

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}

    @property
    def _client_id(self) -> str:
        return self._config.get('reddit_client_id', '')

    @property
    def _client_secret(self) -> str:
        return self._config.get('reddit_client_secret', '')

    @property
    def _username(self) -> str:
        return self._config.get('reddit_username', '')

    @property
    def _password(self) -> str:
        return self._config.get('reddit_password', '')

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._username)

    def get_config_fields(self) -> list:
        return [
            {'key': 'reddit_client_id', 'label': 'Client ID', 'type': 'text'},
            {'key': 'reddit_client_secret', 'label': 'Client Secret', 'type': 'password'},
            {'key': 'reddit_username', 'label': 'Username', 'type': 'text'},
            {'key': 'reddit_password', 'label': 'Password', 'type': 'password'},
        ]

    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                config: Optional[dict] = None, **kwargs) -> dict:
        if config:
            self._config = config

        if not self.is_configured():
            return {'success': False, 'error': _('Reddit not configured')}

        try:
            import praw

            reddit = praw.Reddit(
                client_id=self._client_id,
                client_secret=self._client_secret,
                username=self._username,
                password=self._password,
                user_agent='VeroRunSocialPush/1.0 (by /u/' + self._username + ')',
            )

            # Determine target subreddit from kwargs or use default
            subreddit_name = kwargs.get('subreddit', self.DEFAULT_SUBREDDIT or '')
            if not subreddit_name:
                return {'success': False, 'error': _('No subreddit specified')}

            subreddit = reddit.subreddit(subreddit_name)

            if link_url:
                submission = subreddit.submit(
                    title=title or summary or body[:80],
                    url=link_url,
                )
            else:
                selftext = (summary or body)[:40000]  # Reddit 40k char limit
                submission = subreddit.submit(
                    title=title or summary or body[:80],
                    selftext=selftext,
                )

            return {
                'success': True,
                'post_id': submission.id,
                'url': f'https://reddit.com{submission.permalink}',
                'error': '',
            }
        except ImportError:
            return {'success': False, 'error': _('praw not installed')}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}
