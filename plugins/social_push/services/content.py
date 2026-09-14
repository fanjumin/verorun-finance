#!/usr/bin/env python3
"""Social Push Plugin — 内容模型与平台格式适配（P2）

长文/短文区分：短文(post) 走 title+body 纯文本，按平台字数裁剪；
长文(article) 保留 body_html 富文本（公众号/头条/LinkedIn）。
统一 normalize_payload() 产出 im_gateway gateway.publish() 兼容 payload。
"""
from typing import Dict

# 平台 → 字数/格式约束（适配层元数据，check-config 前端可复用）
PLATFORM_RULES: Dict[str, dict] = {
    'twitter':          {'max_chars': 280,  'mode': 'short', 'media_max': 4},
    'facebook':         {'max_chars': 5000, 'mode': 'short', 'media_max': 10},
    'instagram':        {'max_chars': 2200, 'mode': 'short', 'media_max': 10},
    'linkedin':         {'max_chars': 3000, 'mode': 'both',  'media_max': 9},
    'reddit':           {'max_chars': 40000,'mode': 'short', 'media_max': 1},
    'telegram_channel': {'max_chars': 4096, 'mode': 'short', 'media_max': 10},
    'telegram':         {'max_chars': 4096, 'mode': 'short', 'media_max': 10},
    'weibo':            {'max_chars': 2000, 'mode': 'short', 'media_max': 9},
    'wechat':           {'max_chars': 20000,'mode': 'long',  'media_max': 8},
    'wechat_oa':        {'max_chars': 20000,'mode': 'long',  'media_max': 8},
    'toutiao':          {'max_chars': 20000,'mode': 'long',  'media_max': 3},
}

# 长文支持平台（其余按短文处理）
LONG_FORM_CHANNELS = {'wechat', 'wechat_oa', 'toutiao', 'linkedin'}


def normalize_payload(channel: str, draft: dict) -> dict:
    """按平台约束把草稿归一化为 gateway.publish() payload。

    短文：拼接 title+body，裁剪到 max_chars
    长文：保留 body_html（公众号/头条/LinkedIn 需富文本）
    媒体：按平台上限截断 + Pillow 压缩/规格化（P3 媒体管线）
    """
    rule = PLATFORM_RULES.get(channel, {'max_chars': 500, 'mode': 'short',
                                        'media_max': 1})
    mode = rule['mode']
    media = (draft.get('media') or [])[:rule['media_max']]

    # 媒体处理：截断 + 压缩/规格化（fail-open，失败保留原 URL）
    try:
        from .media import group_media
        media = group_media(media, channel)
    except Exception:
        media = [m.get('url') if isinstance(m, dict) else m
                 for m in media if m]

    cover = draft.get('cover_image_url') or ''
    if cover:
        try:
            from .media import process_image
            cover = process_image(cover, channel)
        except Exception:
            pass

    if mode == 'long' or channel in LONG_FORM_CHANNELS:
        return {
            'title': (draft.get('title') or '')[:120],
            'body_html': draft.get('body_html') or f'<p>{draft.get("body", "")}</p>',
            'body': draft.get('body', ''),
            'summary': (draft.get('summary') or draft.get('body', '')[:100]),
            'media': media,
            'cover_image_url': cover,
        }

    text = ' '.join(x for x in (draft.get('title'), draft.get('body')) if x)
    text = text[:rule['max_chars']]
    return {
        'title': text[:80],
        'body': text,
        'summary': draft.get('summary') or text[:100],
        'media': media,
        'cover_image_url': cover,
    }


def platform_info(channel: str) -> dict:
    """平台元数据（check-config 前端展示用）。"""
    return PLATFORM_RULES.get(channel, {'max_chars': 500, 'mode': 'short',
                                        'media_max': 1})
