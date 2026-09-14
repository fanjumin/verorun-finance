#!/usr/bin/env python3
"""微博 OAuth 2.0 授权码流（Phase 2）。

access_token 长期有效（除非被撤销），无 refresh 机制。
"""
import json
import urllib.request as _ur
from urllib.parse import urlencode

from .base import BaseOAuthProvider
from . import register_oauth


@register_oauth
class WeiboOAuthProvider(BaseOAuthProvider):
    platform = 'weibo'
    SCOPE = ''
    AUTHORIZE_URL = 'https://api.weibo.com/oauth2/authorize'
    TOKEN_URL = 'https://api.weibo.com/oauth2/access_token'

    @property
    def _client_id(self) -> str:
        return self.config.get('weibo_app_key', '')

    @property
    def _client_secret(self) -> str:
        return self.config.get('weibo_app_secret', '')

    def get_authorize_url(self, state: str, redirect_uri: str):
        params = urlencode({
            'client_id': self._client_id,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'state': state,
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        payload = urlencode({
            'grant_type': 'authorization_code',
            'client_id': self._client_id,
            'client_secret': self._client_secret,
            'code': code,
            'redirect_uri': redirect_uri,
        }).encode()
        req = _ur.Request(self.TOKEN_URL, data=payload,
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'Weibo token error: {resp}')
        return {
            'access_token': resp['access_token'],
            'refresh_token': '',
            'expires_in': resp.get('expires_in', 0),
            'uid': str(resp.get('uid', '')),
            'handle': str(resp.get('uid', '')),
        }

    def refresh_token(self, refresh_token: str) -> dict:
        raise NotImplementedError('Weibo access_token is long-lived, no refresh')
