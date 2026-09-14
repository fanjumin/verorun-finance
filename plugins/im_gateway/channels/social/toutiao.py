#!/usr/bin/env python3
"""今日头条社媒渠道适配器（Phase 3）。

OAuth 2.0 授权后 channel_accounts 存 access_token。
发布逻辑与 auth-center/services/toutiao_service.publish_article 一致，
仅 token 来源改为网关账号（service 内部读 system_config 无法注入网关 token）。
"""
import re

from .. import register_channel_adapter
from . import _SocialChannelBase


@register_channel_adapter
class ToutiaoChannel(_SocialChannelBase):
    channel = 'toutiao'
    auth_mode = 'oauth'
    supports_test = False

    def send(self, payload: dict, **kw) -> dict:
        try:
            cfg = self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        access_token = cfg.get('access_token', '')
        if not access_token:
            return {'success': False, 'error': 'Toutiao access token missing'}
        title = payload.get('title', '')
        content_html = (payload.get('content_html', '')
                        or f'<p>{payload.get("body", "")}</p>')
        plain_text = re.sub(r'<[^>]+>', '', content_html).strip()
        try:
            import requests
            body = {
                'title': title,
                'content': content_html,
                'content_plain': plain_text,
                'cover_images': [payload['image_url']] if payload.get('image_url') else [],
                'summary': payload.get('summary') or plain_text[:200],
                'allow_comment': 1,
                'original_type': 0,
            }
            resp = requests.post(
                'https://open-api.toutiao.com/2/article/publish/',
                headers={'Access-Token': access_token,
                         'Content-Type': 'application/json'},
                json=body,
                timeout=30,
            )
            result = resp.json()
            if result.get('err_no', -1) != 0:
                msg = result.get('message', result.get('err_msg', str(result)))
                return {'success': False, 'error': str(msg)[:2000]}
            article_id = (result.get('data') or {}).get('article_id', '')
            return {'success': True, 'post_id': str(article_id), 'url': '', 'error': ''}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
