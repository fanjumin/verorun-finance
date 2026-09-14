#!/usr/bin/env python3
"""Twitter/X OAuth 1.0a 三足授权（Phase 2）。

依赖 tweepy（可选，未安装时抛 ImportError 由上层提示）。
request_token_secret 经返回值随 state 带回（配合 oauth_verifier 换取 access token）。
"""
from .base import BaseOAuthProvider
from . import register_oauth


@register_oauth
class TwitterOAuthProvider(BaseOAuthProvider):
    platform = 'twitter'
    SCOPE = ''
    AUTHORIZE_URL = 'https://api.twitter.com/oauth/authorize'
    TOKEN_URL = 'https://api.twitter.com/oauth/access_token'

    def __init__(self, config: dict = None):
        super().__init__(config)
        self._token_secret = ''

    @property
    def _api_key(self) -> str:
        return self.config.get('twitter_api_key', '')

    @property
    def _api_secret(self) -> str:
        return self.config.get('twitter_api_secret', '')

    def get_authorize_url(self, state: str, redirect_uri: str):
        """OAuth 1.0a：请求 token → 授权页。返回 (url, {tk_secret})"""
        try:
            from tweepy import OAuth1UserHandler
        except ImportError:
            raise RuntimeError('tweepy not installed; Twitter OAuth unavailable')
        auth = OAuth1UserHandler(self._api_key, self._api_secret, callback=redirect_uri)
        url = auth.get_authorization_url()
        self._token_secret = (auth.request_token or {}).get('oauth_token_secret', '')
        return url, {'tk_secret': self._token_secret}

    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        """code 为 oauth_verifier；extras 需含 oauth_token + tk_secret"""
        try:
            from tweepy import OAuth1UserHandler
        except ImportError:
            raise RuntimeError('tweepy not installed; Twitter OAuth unavailable')
        auth = OAuth1UserHandler(self._api_key, self._api_secret, callback=redirect_uri)
        auth.request_token = {
            'oauth_token': extras.get('oauth_token', ''),
            'oauth_token_secret': extras.get('tk_secret', ''),
        }
        access_token, access_token_secret = auth.get_access_token(code)
        return {
            'access_token': access_token,
            'access_token_secret': access_token_secret,
            'refresh_token': '',
            'expires_in': 0,
            'uid': '',
            'handle': '',
        }
