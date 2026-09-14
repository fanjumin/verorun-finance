#!/usr/bin/env python3
"""Reddit OAuth 2.0 授权码流（installable app，scope=submit，支持 refresh token，Phase 2）。

替换旧的 username/password 脚本模式，refresh_token 永久有效。
"""
import json
import base64
import urllib.request as _ur
from urllib.parse import urlencode

from .base import BaseOAuthProvider
from . import register_oauth


@register_oauth
class RedditOAuthProvider(BaseOAuthProvider):
    platform = 'reddit'
    supports_refresh = True
    SCOPE = 'submit,identity'
    AUTHORIZE_URL = 'https://www.reddit.com/api/v1/authorize'
    TOKEN_URL = 'https://www.reddit.com/api/v1/access_token'

    @property
    def _client_id(self) -> str:
        return self.config.get('reddit_client_id', '')

    @property
    def _client_secret(self) -> str:
        return self.config.get('reddit_client_secret', '')

    def get_authorize_url(self, state: str, redirect_uri: str):
        params = urlencode({
            'client_id': self._client_id,
            'response_type': 'code',
            'state': state,
            'redirect_uri': redirect_uri,
            'duration': 'permanent',
            'scope': self.SCOPE,
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        payload = urlencode({
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
        }).encode()
        basic = base64.b64encode(f'{self._client_id}:{self._client_secret}'.encode()).decode()
        req = _ur.Request(self.TOKEN_URL, data=payload, headers={
            'Authorization': f'Basic {basic}',
            'User-Agent': 'verorun-unified-gateway/2.0',
        })
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'Reddit token error: {resp}')

        uid, handle = '', ''
        try:
            me_req = _ur.Request('https://oauth.reddit.com/api/v1/me', headers={
                'Authorization': f"bearer {resp['access_token']}",
                'User-Agent': 'verorun-unified-gateway/2.0',
            })
            me = json.loads(_ur.urlopen(me_req, timeout=10).read())
            uid = str(me.get('id', ''))
            handle = me.get('name', '')
        except Exception:
            pass
        return {
            'access_token': resp['access_token'],
            'refresh_token': resp.get('refresh_token', ''),
            'expires_in': resp.get('expires_in', 0),
            'uid': uid,
            'handle': handle,
        }

    def refresh_token(self, refresh_token: str) -> dict:
        payload = urlencode({
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        }).encode()
        basic = base64.b64encode(f'{self._client_id}:{self._client_secret}'.encode()).decode()
        req = _ur.Request(self.TOKEN_URL, data=payload, headers={
            'Authorization': f'Basic {basic}',
            'User-Agent': 'verorun-unified-gateway/2.0',
        })
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'Reddit refresh error: {resp}')
        return {
            'access_token': resp['access_token'],
            'refresh_token': resp.get('refresh_token', refresh_token),
            'expires_in': resp.get('expires_in', 0),
        }
