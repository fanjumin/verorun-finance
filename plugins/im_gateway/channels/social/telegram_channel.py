#!/usr/bin/env python3
"""Telegram 频道社媒渠道适配器（Phase 3）— 复用 social_push TelegramChannelPushProvider。

统一凭据方案：bot_token 复用 IM 层 telegram 渠道 channel_configs 中已配置的 token
（接入一次，社媒/发布多层级共享）；telegram_channel 目标（@username 或数字 ID）
仍按账号存于 channel_accounts。已存账号中的 telegram_bot_token 作为向后兼容兜底。
"""
from .. import register_channel_adapter
from . import _SocialChannelBase, app_credentials


@register_channel_adapter
class TelegramChannel(_SocialChannelBase):
    channel = 'telegram_channel'
    channel_type = 'social'
    auth_mode = 'client_credential'
    supports_test = False

    def get_config_fields(self):
        return [
            {'key': 'telegram_channel', 'label': 'Channel (@username or numeric ID)',
             'type': 'text'},
        ]

    def send(self, payload: dict, **kw) -> dict:
        try:
            cfg = self._account()
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        try:
            from plugins.social_push.providers.telegram_channel import (
                TelegramChannelPushProvider,
            )
            # 统一凭据：优先复用 IM 层 telegram 的 bot_token（避免重复接入）
            shared = app_credentials('telegram')
            token = shared.get('bot_token', '') or cfg.get('telegram_bot_token', '')
            p = self._publish_params(payload)
            result = TelegramChannelPushProvider({
                'telegram_bot_token': token,
                'telegram_channel': cfg.get('telegram_channel', ''),
            }).publish(**p)
            return result if isinstance(result, dict) else {'success': bool(result)}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
