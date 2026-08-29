#!/usr/bin/env python3
"""LinkedIn push provider — article/post publishing via LinkedIn REST API v2.

Credentials are passed via a config dict (from system_config).
Uses only standard library (urllib), no extra dependencies required.
"""
import json
import urllib.request
from typing import Optional
from i18n import _
from .base import BaseSocialProvider


class LinkedInPushProvider(BaseSocialProvider):
    """LinkedIn API — share article/post as a UGC post."""

    PROVIDER = 'linkedin'
    DISPLAY_NAME = 'LinkedIn'
    ICON = 'in'

    API_BASE = 'https://api.linkedin.com/v2'

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}

    @property
    def _access_token(self) -> str:
        return self._config.get('linkedin_access_token', '')

    @property
    def _client_id(self) -> str:
        return self._config.get('linkedin_client_id', '')

    @property
    def _client_secret(self) -> str:
        return self._config.get('linkedin_client_secret', '')

    def is_configured(self) -> bool:
        return bool(self._access_token)

    def get_config_fields(self) -> list:
        return [
            {'key': 'linkedin_client_id', 'label': 'Client ID', 'type': 'text'},
            {'key': 'linkedin_client_secret', 'label': 'Client Secret', 'type': 'password'},
            {'key': 'linkedin_access_token', 'label': 'Access Token', 'type': 'password'},
        ]

    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                config: Optional[dict] = None, **kwargs) -> dict:
        if config:
            self._config = config

        if not self.is_configured():
            return {'success': False, 'error': _('LinkedIn not configured')}

        try:
            # Get user URN from /userinfo
            req = urllib.request.Request(
                f'{self.API_BASE}/userinfo',
                headers={'Authorization': f'Bearer {self._access_token}'}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            user_info = json.loads(resp.read().decode())
            user_urn = f"urn:li:person:{user_info.get('sub', '')}"

            # 有链接 → ARTICLE（附 article 对象）；无链接 → NONE（纯文本动态）
            share_content = {
                'shareCommentary': {
                    'text': f"{title}\n\n{summary or body[:200]}"
                },
            }
            if link_url:
                share_content['shareMediaCategory'] = 'ARTICLE'
                article = {
                    'source': link_url,
                    'title': title,
                    'description': summary or body[:200],
                }
                if image_url:
                    article['thumbnail'] = image_url
                share_content['article'] = article
            else:
                share_content['shareMediaCategory'] = 'NONE'

            post_body = {
                'author': user_urn,
                'lifecycleState': 'PUBLISHED',
                'specificContent': {
                    'com.linkedin.ugc.ShareContent': share_content
                },
                'visibility': {
                    'com.linkedin.ugc.MemberNetworkVisibility': 'PUBLIC'
                },
            }

            data = json.dumps(post_body).encode()
            req = urllib.request.Request(
                f'{self.API_BASE}/ugcPosts',
                data=data,
                headers={
                    'Authorization': f'Bearer {self._access_token}',
                    'Content-Type': 'application/json',
                    'X-Restli-Protocol-Version': '2.0.0',
                },
                method='POST',
            )
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read().decode())
            post_id = result.get('id', '')

            return {
                'success': True,
                'post_id': post_id,
                'url': link_url or '',
                'error': '',
            }
        except Exception as exc:
            return {'success': False, 'error': str(exc)}
