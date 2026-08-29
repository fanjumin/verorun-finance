#!/usr/bin/env python3
"""WeChat Push Service — assets, drafts, publishing for Official Account.
   This file was renamed from wechat_service.py to avoid collision with OAuth module."""

import json, time, logging, requests
from models import get_db

logger = logging.getLogger(__name__)

WECHAT_API_BASE = 'https://api.weixin.qq.com/cgi-bin'


def _get_config():
    """Read WeChat config from system_config."""
    keys = ['wechat_app_id', 'wechat_app_secret', 'wechat_token']
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM system_config WHERE key IN ({','.join('%s' for _ in keys)})",
            keys
        ).fetchall()
    cfg = {r['key']: r['value'] for r in rows}
    return cfg


def _get_access_token():
    """Get a valid access_token, refreshing if expired (cached in system_config)."""
    cfg = _get_config()
    app_id = cfg.get('wechat_app_id', '')
    app_secret = cfg.get('wechat_app_secret', '')
    if not app_id or not app_secret:
        raise ValueError('微信公众号配置不完整，请在系统设置中配置 AppID 和 AppSecret')

    # Check existing cached token
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key='wechat_access_token'"
        ).fetchone()
        cached_token = row['value'] if row else ''

    # Try the cached token first
    if cached_token:
        resp = requests.get(f'{WECHAT_API_BASE}/getcallbackip?access_token={cached_token}', timeout=10)
        data = resp.json()
        if data.get('errcode') != 40001:
            return cached_token

    # Refresh token
    resp = requests.get(
        f'{WECHAT_API_BASE}/token?grant_type=client_credential&appid={app_id}&secret={app_secret}',
        timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise ValueError(f'获取 access_token 失败: {data.get("errmsg", str(data))}')

    token = data['access_token']
    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_config (key, value, description) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, description=EXCLUDED.description",
            ('wechat_access_token', token, '微信 AccessToken (自动缓存)')
        )
        conn.commit()
    return token


def _call_api(method, path, data=None, params=None):
    """Call WeChat API with automatic token refresh on 40001."""
    token = _get_access_token()
    p = params or {}
    p['access_token'] = token
    url = f'{WECHAT_API_BASE}{path}'

    if method == 'GET':
        resp = requests.get(url, params=p, timeout=15)
    else:
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        resp = requests.post(url, params=p, json=data, headers=headers, timeout=15)

    result = resp.json()
    errcode = result.get('errcode', 0)
    if errcode == 40001:
        logger.info('AccessToken expired, refreshing...')
        with get_db() as conn:
            conn.execute("DELETE FROM system_config WHERE key='wechat_access_token'")
            conn.commit()
        token = _get_access_token()
        p['access_token'] = token
        if method == 'GET':
            resp = requests.get(url, params=p, timeout=15)
        else:
            resp = requests.post(url, params=p, json=data, headers=headers, timeout=15)
        result = resp.json()
    elif errcode != 0:
        logger.warning(f'WeChat API error ({path}): {result}')

    return result


# =============================================
# 素材管理 (Assets)
# =============================================

def upload_image(image_path):
    """Upload a permanent image — returns media_id."""
    token = _get_access_token()
    url = f'{WECHAT_API_BASE}/material/add_material?access_token={token}&type=image'
    with open(image_path, 'rb') as f:
        resp = requests.post(url, files={'media': f}, timeout=30)
    result = resp.json()
    if 'media_id' not in result:
        raise ValueError(f'上传图片失败: {result.get("errmsg", str(result))}')
    return result['media_id']


def upload_article_image(image_source, is_url=True):
    """Upload an image for article body — returns URL to use in article HTML.
    If is_url=True: downloads the URL first, then uploads.
    If is_url=False: uploads local file.
    """
    token = _get_access_token()
    if is_url:
        dl = requests.get(image_source, timeout=30)
        dl.raise_for_status()
        files = {'media': ('image.png', dl.content, 'image/png')}
    else:
        with open(image_source, 'rb') as f:
            files = {'media': f}

    url = f'{WECHAT_API_BASE}/media/uploadimg?access_token={token}'
    resp = requests.post(url, files=files, timeout=30)
    result = resp.json()
    if 'url' not in result:
        raise ValueError(f'上传文章图片失败: {result.get("errmsg", str(result))}')
    return result['url']


# =============================================
# 草稿箱 (Drafts)
# =============================================

def create_draft(title, content_html, author='', digest='', thumb_media_id='',
                 need_open_comment=0, only_fans_can_comment=0):
    """Create a draft article. Returns media_id (draft_id)."""
    body = {
        'articles': [{
            'title': title,
            'content': content_html,
            'author': author or '',
            'digest': digest or '',
            'thumb_media_id': thumb_media_id or '',
            'need_open_comment': need_open_comment,
            'only_fans_can_comment': only_fans_can_comment,
            'content_source_url': '',
        }]
    }
    result = _call_api('POST', '/draft/add', data=body)
    if 'media_id' not in result:
        raise ValueError(f'创建草稿失败: {result.get("errmsg", str(result))}')
    return result['media_id']


def update_draft(media_id, title, content_html, author='', digest='',
                 thumb_media_id='', need_open_comment=0):
    """Update an existing draft."""
    body = {
        'media_id': media_id,
        'articles': [{
            'title': title,
            'content': content_html,
            'author': author or '',
            'digest': digest or '',
            'thumb_media_id': thumb_media_id or '',
            'need_open_comment': need_open_comment,
            'content_source_url': '',
        }],
        'index': 0,
    }
    result = _call_api('POST', '/draft/update', data=body)
    if result.get('errcode', 0) != 0:
        raise ValueError(f'更新草稿失败: {result.get("errmsg", str(result))}')
    return True


def get_draft(media_id):
    """Get a draft by media_id."""
    result = _call_api('POST', '/draft/get', data={'media_id': media_id})
    if 'news_item' not in result:
        raise ValueError(f'获取草稿失败: {result.get("errmsg", str(result))}')
    return result


def list_drafts(offset=0, count=20, no_content=False):
    """List drafts."""
    body = {'offset': offset, 'count': count, 'no_content': 1 if no_content else 0}
    result = _call_api('POST', '/draft/batchget', data=body)
    if 'item_count' not in result:
        raise ValueError(f'获取草稿列表失败: {result.get("errmsg", str(result))}')
    return result


# =============================================
# 发布 (Publishing)
# =============================================

def submit_publish(media_id):
    """Submit a draft for publishing. Returns publish_id."""
    result = _call_api('POST', '/freepublish/submit', data={'media_id': media_id})
    if 'publish_id' not in result:
        raise ValueError(f'发布失败: {result.get("errmsg", str(result))}')
    return result['publish_id']


def get_publish_status(publish_id):
    """Check publish status."""
    result = _call_api('POST', '/freepublish/get', data={'publish_id': publish_id})
    return result


def list_published(offset=0, count=20, no_content=False):
    """List published articles."""
    body = {'offset': offset, 'count': count, 'no_content': 1 if no_content else 0}
    result = _call_api('POST', '/freepublish/batchget', data=body)
    if 'item_count' not in result:
        raise ValueError(f'获取已发布列表失败: {result.get("errmsg", str(result))}')
    return result


def delete_published(article_id, index=0):
    """Delete a published article."""
    result = _call_api('POST', '/freepublish/delete', data={'article_id': article_id, 'index': index})
    if result.get('errcode', 0) != 0:
        raise ValueError(f'删除发布失败: {result.get("errmsg", str(result))}')
    return True


# =============================================
# 预览 (Preview)
# =============================================

def preview_draft(media_id, openid='', wxname=''):
    """Preview a draft to a specific user's phone."""
    if not openid and not wxname:
        raise ValueError('需要提供 openid 或 wxname')
    body = {'touser': openid or wxname, 'msgtype': 'mpnews',
            'mpnews': {'media_id': media_id}}
    result = _call_api('POST', '/message/mass/preview', data=body)
    if result.get('errcode', 0) != 0:
        raise ValueError(f'预览发送失败: {result.get("errmsg", str(result))}')
    return result
