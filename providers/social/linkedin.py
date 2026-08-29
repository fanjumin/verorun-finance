#!/usr/bin/env python3
"""LinkedIn Social Push Provider.

Environment variables:
    LINKEDIN_CLIENT_ID
    LINKEDIN_CLIENT_SECRET
    LINKEDIN_ACCESS_TOKEN
"""
import os, json, urllib.request, urllib.parse
from typing import Optional
from .base import BaseSocialPushProvider


class LinkedInPushProvider(BaseSocialPushProvider):
    """LinkedIn API — share article/post."""

    PROVIDER = 'linkedin'

    API_BASE = 'https://api.linkedin.com/v2'

    def __init__(self):
        self._client_id = os.environ.get('LINKEDIN_CLIENT_ID', '')
        self._client_secret = os.environ.get('LINKEDIN_CLIENT_SECRET', '')
        self._access_token = os.environ.get('LINKEDIN_ACCESS_TOKEN', '')

    def is_configured(self) -> bool:
        return bool(self._access_token)

    def publish(self, title: str, body: str, summary: str = '',
                image_url: str = '', link_url: str = '',
                **kwargs) -> dict:
        if not self.is_configured():
            return {'success': False, 'error': 'LinkedIn not configured'}

        try:
            # Get user URN
            req = urllib.request.Request(
                f'{self.API_BASE}/userinfo',
                headers={'Authorization': f'Bearer {self._access_token}'}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            user_info = json.loads(resp.read().decode())
            user_urn = f"urn:li:person:{user_info.get('sub', '')}"

            post_body = {
                'author': user_urn,
                'lifecycleState': 'PUBLISHED',
                'specificContent': {
                    'com.linkedin.ugc.ShareContent': {
                        'shareCommentary': {
                            'text': f"{title}\n\n{summary or body[:200]}"
                        },
                        'shareMediaCategory': 'ARTICLE',
                    }
                },
                'visibility': {
                    'com.linkedin.ugc.MemberNetworkVisibility': 'PUBLIC'
                },
            }

            if link_url:
                post_body['specificContent']['com.linkedin.ugc.ShareContent']['article'] = {
                    'source': link_url,
                    'title': title,
                    'description': summary or body[:200],
                }
                if image_url:
                    post_body['specificContent']['com.linkedin.ugc.ShareContent']['article']['thumbnail'] = image_url

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
        except Exception as e:
            return {'success': False, 'error': str(e)}
