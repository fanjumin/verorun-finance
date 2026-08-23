#!/usr/bin/env python3
"""Facebook OAuth Provider — Facebook Login (OAuth 2.0).

Environment variables:
    FACEBOOK_APP_ID
    FACEBOOK_APP_SECRET

Scope: email public_profile
"""
import os, urllib.parse, urllib.request, json

from plugins.oauth_config.providers.base import BaseOAuthProvider


class FacebookOAuthProvider(BaseOAuthProvider):
    """Facebook OAuth 2.0 (Graph API v19+)."""

    PROVIDER = 'facebook'

    AUTHORIZE_URL = 'https://www.facebook.com/v19.0/dialog/oauth'
    TOKEN_URL = 'https://graph.facebook.com/v19.0/oauth/access_token'
    USERINFO_URL = 'https://graph.facebook.com/v19.0/me'

    def get_client_id(self) -> str:
        return os.environ.get('FACEBOOK_APP_ID', '')

    def get_client_secret(self) -> str:
        return os.environ.get('FACEBOOK_APP_SECRET', '')

    def get_authorize_url(self, redirect_uri: str, state: str = 'login') -> str:
        params = urllib.parse.urlencode({
            'client_id': self.get_client_id(),
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'email public_profile',
            'state': state or 'login',
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        params = urllib.parse.urlencode({
            'code': code,
            'client_id': self.get_client_id(),
            'client_secret': self.get_client_secret(),
            'redirect_uri': redirect_uri,
        })
        req = urllib.request.Request(f'{self.TOKEN_URL}?{params}')
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read().decode())
            if 'access_token' in result:
                return result
            return {'error': result.get('error', {}).get('message', 'unknown')}
        except Exception as e:
            return {'error': str(e)}

    def get_userinfo(self, access_token: str) -> dict:
        params = urllib.parse.urlencode({
            'fields': 'id,name,picture,email',
            'access_token': access_token,
        })
        req = urllib.request.Request(f'{self.USERINFO_URL}?{params}')
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            info = json.loads(resp.read().decode())
            picture_url = ''
            pic_data = info.get('picture', {}).get('data', {})
            if pic_data and pic_data.get('url'):
                picture_url = pic_data['url']
            return {
                'open_id': info.get('id', ''),
                'nickname': info.get('name', ''),
                'avatar': picture_url,
                'email': info.get('email', ''),
            }
        except Exception as e:
            return {'error': str(e)}
