#!/usr/bin/env python3
"""Social Push Plugin — 统一发布入口（经 im_gateway gateway.publish()）

P1：twitter / weibo / telegram 三平台切换走网关发布。
- 平台适配与 token 来源由 im_gateway 渠道适配器负责。
- 返回结构映射回 social_push 既有契约 {platform, status, media_id, url, message, error}。
"""
import logging

logger = logging.getLogger(__name__)

# social_push 平台 id → im_gateway 渠道 id
PLATFORM_TO_CHANNEL = {
    'twitter': 'twitter',
    'weibo': 'weibo',
    'telegram': 'telegram_channel',
    # 预留：linkedin / reddit / toutiao / wechat 后续阶段切换
}


def publish_multi(channels: list, payload: dict) -> dict:
    """一发多平台。channels 为 social_push 平台 id 列表。

    返回 {platform: {success, post_id, url, error}}，单平台失败不影响其他平台。
    """
    if isinstance(channels, str):
        channels = [channels]
    results = {}
    try:
        from plugins.im_gateway.gateway import gateway
    except Exception:
        logger.exception('[SocialPush] im_gateway unavailable')
        for ch in channels:
            results[ch] = {'success': False, 'error': 'IM Gateway unavailable'}
        return results

    for ch in channels:
        gw_ch = PLATFORM_TO_CHANNEL.get(ch, ch)
        try:
            r = gateway.publish([gw_ch], payload)
            results[ch] = r.get(gw_ch, {})
        except Exception as e:
            logger.exception('[SocialPush] publish failed: %s', ch)
            results[ch] = {'success': False, 'error': str(e)[:2000]}
    return results


def to_social_result(platform: str, result: dict, title: str = '') -> dict:
    """把 gateway 结果映射为 social_push 既有返回契约。"""
    if result.get('success'):
        return {
            'platform': platform,
            'status': 'published',
            'media_id': result.get('post_id', ''),
            'url': result.get('url', ''),
            'message': 'Published',
        }
    return {
        'platform': platform,
        'status': 'failed',
        'error': result.get('error', 'Publish Failed'),
    }
