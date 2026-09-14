#!/usr/bin/env python3
"""微信公众号社媒渠道适配器（Phase 3）。

client_credential 模式：AppID/AppSecret 在系统设置（system_config）配置，
复用 auth-center/services/wechat_push_service 的草稿+发布流程。
"""
import os
import sys

from .. import register_channel_adapter
from . import _SocialChannelBase

# 确保 auth-center（连字符目录，无法直接作为包名）可被 import 为 services.* 包
_AUTH_CENTER_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'auth-center')
if _AUTH_CENTER_DIR not in sys.path:
    sys.path.insert(0, _AUTH_CENTER_DIR)


@register_channel_adapter
class WechatOaChannel(_SocialChannelBase):
    channel = 'wechat_oa'
    auth_mode = 'client_credential'
    supports_test = False

    def get_config_fields(self):
        return []  # AppID/AppSecret 在系统设置配置（client_credential 自动换取 token）

    def send(self, payload: dict, **kw) -> dict:
        title = payload.get('title', '')
        content_html = (payload.get('content_html', '')
                        or f'<p>{payload.get("body", "")}</p>')
        try:
            from services.wechat_push_service import (
                create_draft, submit_publish,
            )
        except ImportError:
            return {'success': False, 'error': 'wechat_push_service unavailable'}
        try:
            media_id = create_draft(title, content_html)
            publish_id = submit_publish(media_id)
            return {'success': True, 'post_id': publish_id, 'url': '', 'error': ''}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
