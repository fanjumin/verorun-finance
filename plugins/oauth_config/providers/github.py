#!/usr/bin/env python3
"""GitHub OAuth Provider.

Environment variables:
    GITHUB_CLIENT_ID
    GITHUB_CLIENT_SECRET

Scope: user:email (read user + primary email)
"""
import os, urllib.parse, urllib.request, json

from plugins.oauth_config.providers.base import BaseOAuthProvider


class GitHubOAuthProvider(BaseOAuthProvider):
    """GitHub OAuth 2.0 (non-standard: token returns form-encoded by default)."""

    PROVIDER = 'github'

    AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
    TOKEN_URL = 'https://github.com/login/oauth/access_token'
    USERINFO_URL = 'https://api.github.com/user'
    EMAILS_URL = 'https://api.github.com/user/emails'

    def get_client_id(self) -> str:
        return os.environ.get('GITHUB_CLIENT_ID', '')

    def get_client_secret(self) -> str:
        return os.environ.get('GITHUB_CLIENT_SECRET', '')

    def get_authorize_url(self, redirect_uri: str, state: str = 'login') -> str:
        params = urllib.parse.urlencode({
            'client_id': self.get_client_id(),
            'redirect_uri': redirect_uri,
            'scope': 'user:email',
            'state': state or 'login',
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        data = {
            'code': code,
            'client_id': self.get_client_id(),
            'client_secret': self.get_client_secret(),
            'redirect_uri': redirect_uri,
        }
        req = urllib.request.Request(
            self.TOKEN_URL,
            data=urllib.parse.urlencode(data).encode(),
            headers={'Accept': 'application/json'},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read().decode())
            if 'access_token' in result:
                return result
            return {'error': result.get('error_description', result.get('error', 'unknown'))}
        except Exception as e:
            return {'error': str(e)}

    def get_userinfo(self, access_token: str) -> dict:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/vnd.github.v3+json',
        }
        # Fetch user profile
        req = urllib.request.Request(self.USERINFO_URL, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            user = json.loads(resp.read().decode())
        except Exception as e:
            return {'error': str(e)}

        # Fetch primary email
        email = ''
        try:
            req2 = urllib.request.Request(self.EMAILS_URL, headers=headers)
            resp2 = urllib.request.urlopen(req2, timeout=10)
            emails = json.loads(resp2.read().decode())
            primary = next((e for e in emails if e.get('primary')), None)
            if primary:
                email = primary.get('email', '')
        except Exception:
            pass

        return {
            'open_id': str(user.get('id', '')),
            'nickname': user.get('login', ''),
            'avatar': user.get('avatar_url', ''),
            'email': email,
        }
