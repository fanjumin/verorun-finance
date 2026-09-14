#!/usr/bin/env python3
"""今日头条/抖音开放平台 OAuth 2.0 授权码流（Phase 2，支持 refresh token）。"""
import json
import urllib.request as _ur
from urllib.parse import urlencode

from .base import BaseOAuthProvider
from . import register_oauth


@register_oauth
class ToutiaoOAuthProvider(BaseOAuthProvider):
    platform = 'toutiao'
    supports_refresh = True
    SCOPE = 'user_info,toutiao.video.publish'
    AUTHORIZE_URL = 'https://open.snssdk.com/oauth/authorize'
    TOKEN_URL = 'https://open.snssdk.com/oauth/access_token'

    @property
    def _client_id(self) -> str:
        return self.config.get('toutiao_app_id', '')

    @property
    def _client_secret(self) -> str:
        return self.config.get('toutiao_app_secret', '')

    def get_authorize_url(self, state: str, redirect_uri: str):
        params = urlencode({
            'client_key': self._client_id,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'state': state,
            'scope': self.SCOPE,
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        payload = urlencode({
            'client_key': self._client_id,
            'client_secret': self._client_secret,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
        }).encode()
        req = _ur.Request(self.TOKEN_URL, data=payload,
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'Toutiao token error: {resp}')
        return {
            'access_token': resp['access_token'],
            'refresh_token': resp.get('refresh_token', ''),
            'expires_in': resp.get('expires_in', 0),
            'uid': str(resp.get('open_id', '') or resp.get('user_id', '')),
            'handle': str(resp.get('user_id', '')),
        }

    def refresh_token(self, refresh_token: str) -> dict:
        payload = urlencode({
            'client_key': self._client_id,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        }).encode()
        req = _ur.Request(self.TOKEN_URL, data=payload,
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'Toutiao refresh error: {resp}')
        return {
            'access_token': resp['access_token'],
            'refresh_token': resp.get('refresh_token', refresh_token),
            'expires_in': resp.get('expires_in', 0),
        }
