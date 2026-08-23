#!/usr/bin/env python3
"""site_builder — internal service client (v2.1.0).

插件数据库解耦后，cms_blocks / cms_posts / 品牌信息等主库共享数据不再直连
主库，改为调用 main_site 提供的内部 API（/api/internal/*）。读操作带 LRU
缓存与超时兜底，写操作（draft blocks / documents / publish）实时透传，
避免主站短暂不可用时插件功能完全失效。

环境变量：
    MAIN_SITE_INTERNAL_URL  内部 API 基地址（默认 http://127.0.0.1:8083）
    INTERNAL_SERVICE_TOKEN  内部服务令牌（与 main_site 配置一致）
"""

import os
import time
import json
import urllib.error
import urllib.parse
import urllib.request

_BASE = os.environ.get('MAIN_SITE_INTERNAL_URL', 'http://127.0.0.1:8083')
_TOKEN = os.environ.get('INTERNAL_SERVICE_TOKEN', '')

# key -> (fetched_at, value)
_cache = {}

# key 缓存有效期（秒）
_CACHE_TTL = {
    'brand': 300,
    'draft_blocks': 30,
    'draft_documents': 30,
    'page_blocks': 15,
}

_DEFAULT_BRAND = {
    'site_name': 'VeroRun',
    'tagline': '',
    'primary_color': '#1890ff',
    'secondary_color': '',
    'logo_url': '',
    'favicon_url': '',
}


def _http_get(path: str, params: dict = None, timeout: float = 5.0):
    """GET 内部 API，返回解析后的 dict/list。失败抛异常由调用方兜底。"""
    url = _BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'X-Internal-Token': _TOKEN})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8') or '{}')


def _http_post(path: str, payload: dict, timeout: float = 10.0):
    """POST JSON 到内部 API，返回解析后的 dict。失败抛异常由调用方兜底。"""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        _BASE + path,
        data=data,
        headers={'X-Internal-Token': _TOKEN, 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8') or '{}')


def _cached(key: str, ttl: int, fetch):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fetch()
    _cache[key] = (now, value)
    return value


def clear_cache():
    """写操作后调用，使读取缓存失效。"""
    _cache.clear()


# ── 读操作 ────────────────────────────────────────────────

def get_brand_settings() -> dict:
    """品牌设置（带缓存 + 默认兜底）。"""
    def _f():
        data = _http_get('/api/internal/brand') or {}
        if not data or 'site_name' not in data:
            return dict(_DEFAULT_BRAND)
        return data
    try:
        return _cached('brand', _CACHE_TTL['brand'], _f)
    except Exception:
        return dict(_DEFAULT_BRAND)


def get_draft_blocks(page: str = None) -> list:
    """Draft blocks（is_published=0），可选按 page 过滤。"""
    def _f():
        params = {}
        if page:
            params['page'] = page
        data = _http_get('/api/internal/cms/draft-blocks', params)
        return data if isinstance(data, list) else []
    try:
        return _cached('draft_blocks', _CACHE_TTL['draft_blocks'], _f)
    except Exception:
        return []


def get_draft_documents() -> list:
    """Draft legal documents（slug/title/content）。"""
    def _f():
        data = _http_get('/api/internal/cms/draft-documents')
        return data if isinstance(data, list) else []
    try:
        return _cached('draft_documents', _CACHE_TTL['draft_documents'], _f)
    except Exception:
        return []


def get_page_blocks(page: str) -> list:
    """页面全部 blocks（含已发布），供 LLM 修改上下文使用。"""
    def _f():
        data = _http_get('/api/internal/cms/page-blocks', {'page': page})
        return data if isinstance(data, list) else []
    try:
        return _cached(f'page_blocks_{page}', _CACHE_TTL['page_blocks'], _f)
    except Exception:
        return []


# ── 写操作（实时透传，不缓存） ────────────────────────────

def replace_draft_blocks(page: str, blocks: list, is_published: int = 0) -> dict:
    """幂等替换区块：可选按 page 清空后重写，或全局重写（page 为空）。

    is_published: 目标状态（0=草稿，1=生产）。
    """
    return _http_post('/api/internal/cms/draft-blocks/replace', {
        'page': page or None,
        'blocks': blocks,
        'is_published': is_published,
    })


def update_block(block_id, field: str, value) -> dict:
    """更新单个草稿区块字段（field 白名单由 main_site 校验）。"""
    return _http_post('/api/internal/cms/blocks/update', {
        'block_id': block_id, 'field': field, 'value': value,
    })


def update_block_order(order: list) -> dict:
    """批量更新草稿区块排序。"""
    return _http_post('/api/internal/cms/blocks/order', {'order': order})


def delete_block(block_id) -> dict:
    """软删除草稿区块（extra_json.deleted=true）。"""
    return _http_post('/api/internal/cms/blocks/delete', {'block_id': block_id})


def add_block(**kwargs) -> dict:
    """在指定位置插入新草稿区块。"""
    return _http_post('/api/internal/cms/blocks/add', kwargs)


def write_document(slug: str, title: str, content: str, is_published: int = 0) -> dict:
    """UPSERT 法律文档到 cms_posts（默认草稿 is_published=0）。"""
    return _http_post('/api/internal/cms/documents', {
        'slug': slug, 'title': title, 'content': content, 'is_published': is_published,
    })


def publish_draft() -> dict:
    """发布草稿：cms_blocks / cms_posts 的 is_published 0→1。"""
    return _http_post('/api/internal/cms/publish', {})
