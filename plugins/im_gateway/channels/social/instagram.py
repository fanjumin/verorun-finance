#!/usr/bin/env python3
"""Instagram 社媒渠道适配器（Phase 3）。

OAuth 2.0 授权已完成（复用 Facebook 应用，定位 instagram_business_account，
存 channel_accounts）。发布需 content_publish 容器流程，待接入平台后开发；
当前 send 返回明确占位提示。
"""
from .. import register_channel_adapter
from . import _SocialChannelBase


@register_channel_adapter
class InstagramChannel(_SocialChannelBase):
    channel = 'instagram'
    auth_mode = 'oauth'
    supports_test = False

    def send(self, payload: dict, **kw) -> dict:
        try:
            self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        return {'success': False,
                'error': 'Instagram publishing requires content_publish flow; '
                         'pending development'}
