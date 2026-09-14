#!/usr/bin/env python3
"""LinkedIn 社媒渠道适配器（Phase 3）— 复用 social_push LinkedInPushProvider。

OAuth 2.0 授权后 channel_accounts 存 access_token；
app 凭据（linkedin_client_id / linkedin_client_secret）来自 channel_configs。
"""
from .. import register_channel_adapter
from . import _SocialChannelBase, app_credentials


@register_channel_adapter
class LinkedInChannel(_SocialChannelBase):
    channel = 'linkedin'
    auth_mode = 'oauth'
    supports_test = False

    def send(self, payload: dict, **kw) -> dict:
        try:
            cfg = self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        try:
            from plugins.social_push.providers.linkedin import LinkedInPushProvider
            merged = {
                **app_credentials(self.channel),
                'linkedin_access_token': cfg.get('access_token', ''),
            }
            p = self._publish_params(payload)
            result = LinkedInPushProvider(merged).publish(**p)
            return result if isinstance(result, dict) else {'success': bool(result)}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
