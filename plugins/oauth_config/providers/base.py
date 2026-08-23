#!/usr/bin/env python3
"""Base class for OAuth providers (non-authlib, direct HTTP).

Each provider handles its own authorize URL generation and token/userinfo exchange,
since Google/GitHub/Facebook all use different OAuth 2.0 implementations.
"""
from abc import ABC, abstractmethod
import urllib.parse, urllib.request, json
from typing import Optional


class BaseOAuthProvider(ABC):
    """Abstract base for OAuth 2.0 providers.

    Subclasses MUST set PROVIDER class variable (e.g. 'google', 'github', 'facebook').
    """

    PROVIDER: str = ''  # set by subclass

    AUTHORIZE_URL: str = ''
    TOKEN_URL: str = ''
    USERINFO_URL: str = ''

    @abstractmethod
    def get_client_id(self) -> str:
        """Return the OAuth client ID (app ID)."""
        ...

    @abstractmethod
    def get_client_secret(self) -> str:
        """Return the OAuth client secret."""
        ...

    def get_authorize_url(self, redirect_uri: str, state: str = 'login') -> str:
        """Generate the OAuth authorize URL for this provider.

        Args:
            redirect_uri: Full callback URL (https://...)
            state: CSRF state parameter

        Returns:
            Full authorize URL to redirect the user to.
        """
        params = urllib.parse.urlencode({
            'client_id': self.get_client_id(),
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'state': state,
        })
        return f'{self.AUTHORIZE_URL}?{params}'

    @abstractmethod
    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange authorization code for access token.

        Returns:
            dict with at least 'access_token' key on success,
            or {'error': '...'} on failure.
        """
        ...

    @abstractmethod
    def get_userinfo(self, access_token: str) -> dict:
        """Fetch user info using the access token.

        Returns:
            dict with keys: open_id, nickname, avatar, email (optional)
        """
        ...

    def get_user_by_code(self, code: str, redirect_uri: str) -> dict:
        """Full flow: exchange code → fetch userinfo.

        Returns dict with open_id, nickname, avatar, email (where available),
        or {'error': '...'} on failure.
        """
        token_data = self.exchange_code(code, redirect_uri)
        if 'error' in token_data:
            return token_data
        access_token = token_data.get('access_token', '')
        if not access_token:
            return {'error': f'{self.PROVIDER}: no access_token in response'}
        return self.get_userinfo(access_token)

    def is_configured(self) -> bool:
        """Check if this provider has credentials configured."""
        return bool(self.get_client_id())

    def _post_form(self, url: str, data: dict, headers: Optional[dict] = None) -> dict:
        """POST application/x-www-form-urlencoded and parse JSON response."""
        encoded = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=encoded, headers=headers or {})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read().decode())
        except Exception as e:
            return {'error': str(e)}

    def _get_json(self, url: str, headers: Optional[dict] = None) -> dict:
        """GET and parse JSON response."""
        req = urllib.request.Request(url, headers=headers or {})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read().decode())
        except Exception as e:
            return {'error': str(e)}
