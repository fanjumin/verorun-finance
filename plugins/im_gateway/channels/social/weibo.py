#!/usr/bin/env python3
"""微博社媒渠道适配器（Phase 3）。

OAuth 2.0 授权后 channel_accounts 存 access_token（长期有效）。
发布逻辑与 auth-center/services/weibo_service.publish_weibo 一致，
仅 token 来源改为网关账号（service 内部读 system_config 无法注入网关 token）。
"""
from .. import register_channel_adapter
from . import _SocialChannelBase


@register_channel_adapter
class WeiboChannel(_SocialChannelBase):
    channel = 'weibo'
    auth_mode = 'oauth'
    supports_test = False

    def send(self, payload: dict, **kw) -> dict:
        try:
            cfg = self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        access_token = cfg.get('access_token', '')
        if not access_token:
            return {'success': False, 'error': 'Weibo access token missing'}
        text = (payload.get('text')
                or payload.get('title')
                or payload.get('body') or '')
        image_url = payload.get('image_url', '')
        try:
            import requests
            if image_url:
                resp = requests.post(
                    'https://api.weibo.com/2/statuses/upload_url_text.json',
                    params={'access_token': access_token, 'status': text,
                            'url': image_url},
                    timeout=30,
                )
            else:
                resp = requests.post(
                    'https://api.weibo.com/2/statuses/share.json',
                    params={'access_token': access_token, 'status': text},
                    timeout=15,
                )
            result = resp.json()
            if 'error' in result:
                return {'success': False,
                        'error': str(result.get('error', 'Weibo API error'))[:2000]}
            return {'success': True, 'post_id': str(result.get('id', '')),
                    'url': '', 'error': ''}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
