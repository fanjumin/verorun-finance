#!/usr/bin/env python3
"""Weibo Service — publish text+image posts to Weibo via Open API."""

import logging, json, requests, base64
from models import get_db

logger = logging.getLogger(__name__)

WEIBO_API_BASE = 'https://api.weibo.com/2'


def _get_config():
    """Read Weibo config from system_config."""
    keys = ['weibo_app_key', 'weibo_app_secret', 'weibo_access_token']
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM system_config WHERE key IN ({','.join('%s' for _ in keys)})",
            keys
        ).fetchall()
    cfg = {r['key']: r['value'] for r in rows}
    return cfg


def is_configured():
    """Check if Weibo credentials are set."""
    cfg = _get_config()
    return bool(cfg.get('weibo_app_key') and cfg.get('weibo_access_token'))


def publish_weibo(text, image_url=None):
    """Publish a Weibo post with optional image.
    
    Args:
        text: Weibo content (≤140 chars recommended, but API supports longer)
        image_url: URL of image to attach (optional)
    
    Returns:
        dict with 'id' (weibo ID) on success
    """
    cfg = _get_config()
    access_token = cfg.get('weibo_access_token', '')
    if not access_token:
        raise ValueError('微博 Access Token 未配置')

    if image_url:
        # Step 1: Upload image via URL (need to download first, then upload to Weibo)
        logger.info(f'Downloading image from {image_url}')
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
        img_data = img_resp.content

        # Get filename from URL
        filename = image_url.split('/')[-1].split('?')[0] or 'image.jpg'
        if not filename.endswith(('.jpg', '.jpeg', '.png', '.gif')):
            filename += '.jpg'

        # Upload image to Weibo
        logger.info('Uploading image to Weibo...')
        upload_url = f'{WEIBO_API_BASE}/statuses/upload_url_text.json'
        resp = requests.post(upload_url, params={
            'access_token': access_token,
            'status': text,
            'url': image_url,
        }, timeout=30)
        result = resp.json()
    else:
        # Text-only post
        logger.info('Publishing text-only Weibo...')
        share_url = f'{WEIBO_API_BASE}/statuses/share.json'
        resp = requests.post(share_url, params={
            'access_token': access_token,
            'status': text,
        }, timeout=15)
        result = resp.json()

    if 'id' not in result and 'error' in result:
        raise ValueError(f'微博发布失败: {result.get("error", str(result))}')
    if 'id' not in result and 'created_at' not in result:
        raise ValueError(f'微博发布返回异常: {result}')

    weibo_id = result.get('id', '')
    logger.info(f'Weibo published: id={weibo_id}')
    return {'id': str(weibo_id) if weibo_id else '', 'result': result}
