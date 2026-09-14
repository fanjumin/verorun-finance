#!/usr/bin/env python3
"""Instagram OAuth 2.0（Phase 2）。

复用 Facebook 开发者应用（facebook_app_id / facebook_app_secret），
要求 IG 商业/创作者账号已绑定 FB Page。
链路：code → 长效 user token → 定位 instagram_business_account.id。
"""
import json
import urllib.request as _ur
from urllib.parse import urlencode

from .base import BaseOAuthProvider
from . import register_oauth

_GRAPH_BASE = 'https://graph.facebook.com/v21.0'


@register_oauth
class InstagramOAuthProvider(BaseOAuthProvider):
    platform = 'instagram'
    SCOPE = 'instagram_basic,instagram_content_publish,pages_show_list'
    AUTHORIZE_URL = 'https://www.facebook.com/v21.0/dialog/oauth'
    TOKEN_URL = f'{_GRAPH_BASE}/oauth/access_token'

    @property
    def _client_id(self) -> str:
        return (self.config.get('instagram_app_id')
                or self.config.get('facebook_app_id') or '')

    @property
    def _client_secret(self) -> str:
        return (self.config.get('instagram_app_secret')
                or self.config.get('facebook_app_secret') or '')

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
        short_params = urlencode({
            'client_id': self._client_id,
            'client_secret': self._client_secret,
            'redirect_uri': redirect_uri,
            'code': code,
        })
        short_resp = json.loads(_ur.urlopen(
            f'{self.TOKEN_URL}?{short_params}', timeout=15).read())
        short_token = short_resp.get('access_token', '')
        if not short_token:
            raise RuntimeError(f'Instagram short token error: {short_resp}')

        # 短效 → 长效 user token
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
            raise RuntimeError(f'Instagram long token error: {long_resp}')

        # 定位 IG 商业号（需 FB Page 已绑定 IG 账号）
        uid, handle = '', ''
        try:
            pages_req = _ur.Request(
                f'{_GRAPH_BASE}/me/accounts?fields=id,name,'
                f'instagram_business_account{{id,username}}&access_token={access_token}')
            pages = json.loads(_ur.urlopen(pages_req, timeout=10).read())
            for pg in pages.get('data', []):
                ig = (pg.get('instagram_business_account') or {})
                if ig.get('id'):
                    uid = str(ig['id'])
                    handle = ig.get('username') or pg.get('name', '')
                    break
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
        raise NotImplementedError('Instagram reuses Facebook token exchange flow')
