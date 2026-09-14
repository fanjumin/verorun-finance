#!/usr/bin/env python3
"""Social Push Plugin — AI 创作能力（P2，复用公共 LLM 服务）

AI 文案/配图走全站公共服务 services.ai_content_generator（公共 LLM 服务），
不在插件内私有实现。本模块仅做长文/短文模式封装与封面本地化。
"""
import json
import logging
import os
import uuid

from i18n import _

logger = logging.getLogger(__name__)


def ai_generate_content(topic: str, content_mode: str = 'short',
                        content_type: str = 'article',
                        temperature: float = 0.7) -> dict:
    """按长文/短文模式生成内容。

    - content_mode='short'：生成社交短文（标题 + 摘要 + 正文，≤500 字通用短文模板）
    - content_mode='long' ：生成文章（标题 + 富文本正文 + 摘要）
    返回 {title, body, body_html, summary}
    """
    try:
        from services.ai_content_generator import generate_article
    except Exception as e:
        logger.exception('[SocialPush] ai_content_generator unavailable')
        raise RuntimeError(_('AI service unavailable: {}').format(str(e)))

    result = generate_article(topic, content_type, temperature) or {}
    title = result.get('title') or topic
    body = result.get('content') or result.get('body') or ''
    body_html = result.get('content_html') or result.get('body_html') or ''
    summary = result.get('summary') or body[:100]

    if content_mode == 'short':
        body = body[:500]
        body_html = body_html or f'<p>{body}</p>'
    return {
        'title': title[:120],
        'body': body,
        'body_html': body_html,
        'summary': summary,
    }


def ai_generate_cover(title: str, prompt: str = '') -> str:
    """生成封面图，下载到本地 temp，返回本地 URL（不暴露外部 OSS URL）。"""
    try:
        from services.ai_content_generator import generate_cover_image
    except Exception as e:
        logger.exception('[SocialPush] ai_content_generator unavailable')
        raise RuntimeError(_('AI image service unavailable: {}').format(str(e)))

    oss_url = generate_cover_image(title, prompt or title)
    # 下载到本地，沿用现有 /generate-image 的存储约定
    _auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'auth-center')
    save_dir = os.path.join(_auth_dir, '..', 'admin', 'static', 'uploads', 'temp')
    os.makedirs(save_dir, exist_ok=True)

    import urllib.request
    img_data = urllib.request.urlopen(oss_url, timeout=30).read()
    ext = '.jpg'
    if 'png' in oss_url.lower():
        ext = '.png'
    elif 'webp' in oss_url.lower():
        ext = '.webp'
    filename = f'{uuid.uuid4().hex}{ext}'
    with open(os.path.join(save_dir, filename), 'wb') as f:
        f.write(img_data)
    return f'/static/uploads/temp/{filename}'
