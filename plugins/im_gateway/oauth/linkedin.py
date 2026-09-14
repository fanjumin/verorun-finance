#!/usr/bin/env python3
"""LinkedIn OAuth 2.0 授权码流（w_member_social，支持 refresh token，Phase 2）。"""
import json
import urllib.request as _ur
from urllib.parse import urlencode

from .base import BaseOAuthProvider
from . import register_oauth


@register_oauth
class LinkedInOAuthProvider(BaseOAuthProvider):
    platform = 'linkedin'
    supports_refresh = True
    SCOPE = 'w_member_social,profile,openid,email'
    AUTHORIZE_URL = 'https://www.linkedin.com/oauth/v2/authorization'
    TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'

    @property
    def _client_id(self) -> str:
        return self.config.get('linkedin_client_id', '')

    @property
    def _client_secret(self) -> str:
        return self.config.get('linkedin_client_secret', '')

    def get_authorize_url(self, state: str, redirect_uri: str):
        params = urlencode({
            'response_type': 'code',
            'client_id': self._client_id,
            'redirect_uri': redirect_uri,
            'scope': self.SCOPE,
            'state': state,
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        payload = urlencode({
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
            'client_id': self._client_id,
            'client_secret': self._client_secret,
        }).encode()
        req = _ur.Request(self.TOKEN_URL, data=payload,
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'LinkedIn token error: {resp}')

        access_token = resp['access_token']
        uid, handle = '', ''
        try:
            ui_req = _ur.Request('https://api.linkedin.com/v2/userinfo',
                                 headers={'Authorization': f'Bearer {access_token}'})
            user_info = json.loads(_ur.urlopen(ui_req, timeout=10).read())
            uid = str(user_info.get('sub', ''))
            handle = user_info.get('name') or user_info.get('given_name') or ''
        except Exception:
            pass
        return {
            'access_token': access_token,
            'refresh_token': resp.get('refresh_token', ''),
            'expires_in': resp.get('expires_in', 0),
            'uid': uid,
            'handle': handle,
        }

    def refresh_token(self, refresh_token: str) -> dict:
        payload = urlencode({
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': self._client_id,
            'client_secret': self._client_secret,
        }).encode()
        req = _ur.Request(self.TOKEN_URL, data=payload,
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        if 'access_token' not in resp:
            raise RuntimeError(f'LinkedIn refresh error: {resp}')
        return {
            'access_token': resp['access_token'],
            'refresh_token': resp.get('refresh_token', refresh_token),
            'expires_in': resp.get('expires_in', 0),
        }
