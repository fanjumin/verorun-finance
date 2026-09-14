#!/usr/bin/env python3
"""Twitter/X 社媒渠道适配器（Phase 3）— 复用 social_push TwitterPushProvider。

OAuth 1.0a 授权后 channel_accounts 存 access_token / access_token_secret；
app 凭据（twitter_api_key / twitter_api_secret）来自 channel_configs。
"""
from .. import register_channel_adapter
from . import _SocialChannelBase, app_credentials


@register_channel_adapter
class TwitterChannel(_SocialChannelBase):
    channel = 'twitter'
    auth_mode = 'oauth'
    supports_test = False

    def send(self, payload: dict, **kw) -> dict:
        try:
            cfg = self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        try:
            from plugins.social_push.providers.twitter import TwitterPushProvider
            merged = {
                **app_credentials(self.channel),
                'twitter_access_token': cfg.get('access_token', ''),
                'twitter_access_secret': cfg.get('access_token_secret', ''),
            }
            p = self._publish_params(payload)
            result = TwitterPushProvider(merged).publish(**p)
            return result if isinstance(result, dict) else {'success': bool(result)}
        except ImportError:
            return {'success': False, 'error': 'tweepy not installed'}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
