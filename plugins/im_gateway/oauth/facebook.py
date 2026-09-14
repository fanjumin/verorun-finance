#!/usr/bin/env python3
"""Facebook OAuth 2.0 授权码流（Phase 2）。

链路：code → 短效 user token → 长效 user token（fb_exchange_token）。
Page token 由 facebook 社媒适配器在发布时用长效 token 调 /me/accounts 获取。
config 键：facebook_app_id / facebook_app_secret。
"""
import json
import urllib.request as _ur
from urllib.parse import urlencode

from .base import BaseOAuthProvider
from . import register_oauth

_GRAPH_BASE = 'https://graph.facebook.com/v21.0'


@register_oauth
class FacebookOAuthProvider(BaseOAuthProvider):
    platform = 'facebook'
    SCOPE = 'pages_show_list,pages_manage_posts,pages_read_engagement'
    AUTHORIZE_URL = 'https://www.facebook.com/v21.0/dialog/oauth'
    TOKEN_URL = f'{_GRAPH_BASE}/oauth/access_token'

    @property
    def _client_id(self) -> str:
        return self.config.get('facebook_app_id', '')

    @property
    def _client_secret(self) -> str:
        return self.config.get('facebook_app_secret', '')

    def get_authorize_url(self, state: str, redirect_uri: str):
        params = urlencode({
            'client_id': self._client_id,
            'redirect_uri': redirect_uri,
            'scope': self.SCOPE,
            'state': state,
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        # code → 短效 user token
        short_token = self._fetch_token(code, redirect_uri)
        # 短效 → 长效 user token（60 天）
        long_params = urlencode({
            'grant_type': 'fb_exchange_token',
            'client_id': self._client_id,
            'client_secret': self._client_secret,
            'fb_exchange_token': short_token,
        })
        long_resp = json.loads(_ur.urlopen(
            f'{self.TOKEN_URL}?{long_params}', timeout=15).read())
        access_token = long_resp.get('access_token', '')
        if not access_token:
            raise RuntimeError(f'Facebook long token error: {long_resp}')

        uid, handle = '', ''
        try:
            me_req = _ur.Request(
                f'{_GRAPH_BASE}/me?fields=id,name&access_token={access_token}')
            me = json.loads(_ur.urlopen(me_req, timeout=10).read())
            uid = str(me.get('id', ''))
            handle = me.get('name', '')
        except Exception:
            pass
        return {
            'access_token': access_token,
            'refresh_token': '',
            'expires_in': long_resp.get('expires_in', 0),
            'uid': uid,
            'handle': handle,
        }

    def refresh_token(self, refresh_token: str) -> dict:
        raise NotImplementedError('Facebook uses fb_exchange_token; use exchange_code flow')

    def _fetch_token(self, code: str, redirect_uri: str) -> str:
        params = urlencode({
            'client_id': self._client_id,
            'client_secret': self._client_secret,
            'redirect_uri': redirect_uri,
            'code': code,
        })
        resp = json.loads(_ur.urlopen(f'{self.TOKEN_URL}?{params}', timeout=15).read())
        token = resp.get('access_token', '')
        if not token:
            raise RuntimeError(f'Facebook token error: {resp}')
        return token
