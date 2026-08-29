#!/usr/bin/env python3
"""Toutiao (今日头条) Service — publish articles via 头条号开放平台 API."""

import logging, json, requests, hashlib, time
from models import get_db

logger = logging.getLogger(__name__)


def _get_config():
    """Read Toutiao config from system_config."""
    keys = ['toutiao_app_id', 'toutiao_app_secret', 'toutiao_access_token']
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM system_config WHERE key IN ({','.join('%s' for _ in keys)})",
            keys
        ).fetchall()
    return {r['key']: r['value'] for r in rows}


def is_configured():
    """Check if Toutiao credentials are set."""
    cfg = _get_config()
    return bool(cfg.get('toutiao_app_id') and cfg.get('toutiao_access_token'))


def publish_article(title, content_html, cover_url='', summary=''):
    """Publish an article to 今日头条 (头条号).
    
    Uses 头条号开放平台 content publishing API.
    Args:
        title: Article title
        content_html: HTML content body
        cover_url: Optional cover image URL
        summary: Optional article summary
    
    Returns:
        dict with article_id on success
    """
    cfg = _get_config()
    access_token = cfg.get('toutiao_access_token', '')
    if not access_token:
        raise ValueError('今日头条 Access Token 未配置')

    # 头条号 API endpoint for article publishing
    api_url = 'https://open-api.toutiao.com/2/article/publish/'

    # Strip HTML tags for plain text version if needed
    import re
    plain_text = re.sub(r'<[^>]+>', '', content_html).strip()

    body = {
        'title': title,
        'content': content_html,
        'content_plain': plain_text,
        'cover_images': [cover_url] if cover_url else [],
        'summary': summary or plain_text[:200],
        'allow_comment': 1,
        'original_type': 0,  # 0=转载 1=原创
    }

    headers = {
        'Access-Token': access_token,
        'Content-Type': 'application/json',
    }

    logger.info(f'Publishing to Toutiao: {title[:40]}...')
    resp = requests.post(api_url, headers=headers, json=body, timeout=30)
    result = resp.json()

    err_no = result.get('err_no', -1)
    if err_no != 0:
        err_msg = result.get('message', result.get('err_msg', str(result)))
        raise ValueError(f'今日头条发布失败: {err_msg}')

    data = result.get('data', {})
    article_id = data.get('article_id', '')
    logger.info(f'Toutiao published: article_id={article_id}')
    return {'id': str(article_id), 'data': data}
