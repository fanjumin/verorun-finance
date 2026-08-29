#!/usr/bin/env python3
"""
1688 按图搜索服务

功能：1. 本地图片 → Base64 → 调用 1688 按图搜索
2. 图片 URL → 下载 → Base64 → 调用
3. 解析搜索结果
"""

import base64
import logging
import os
import urllib.request
from typing import Dict, Any, Optional, Tuple

from .alibaba_client_v2 import search_product_by_image

logger = logging.getLogger(__name__)


def search_by_image_file(image_path: str, access_token: str,
                         app_key: str = None, app_secret: str = None,
                         page: int = 1, size: int = 20) -> Dict[str, Any]:
    """本地图片文件 → 按图搜索

    Args:
        image_path: 本地图片路径
        access_token: OAuth access_token
        app_key: AppKey
        app_secret: AppSecret
        page: 页码
        size: 每页条数

    Returns:
        {'success': bool, 'products': [...], 'error': str|None}
    """
    if not os.path.exists(image_path):
        return {'success': False, 'products': [], 'error': f'Picture file does not exist: {image_path}'}

    try:
        with open(image_path, 'rb') as f:
            image_data = f.read()
        image_base64 = base64.b64encode(image_data).decode('utf-8')
    except Exception as e:
        logger.error(f"读取图片失败: {e}")
        return {'success': False, 'products': [], 'error': str(e)}

    return _search_by_base64(image_base64, access_token, app_key, app_secret, page, size)


def search_by_image_url(image_url: str, access_token: str,
                        app_key: str = None, app_secret: str = None,
                        page: int = 1, size: int = 20,
                        max_size_bytes: int = 5 * 1024 * 1024) -> Dict[str, Any]:
    """图片 URL → 下载 → 按图搜索

    Args:
        image_url: 图片 URL
        access_token: OAuth access_token
        app_key: AppKey
        app_secret: AppSecret
        page: 页码
        size: 每页条数
        max_size_bytes: 最大图片大小（默认 5MB）

    Returns:
        {'success': bool, 'products': [...], 'error': str|None}
    """
    try:
        req = urllib.request.Request(
            image_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            image_data = resp.read()
            if len(image_data) > max_size_bytes:
                return {'success': False, 'products': [],
                        'error': f'Picture too large: {len(image_data)} > {max_size_bytes} bytes'}
        image_base64 = base64.b64encode(image_data).decode('utf-8')
    except Exception as e:
        logger.error(f"下载图片失败: {e}")
        return {'success': False, 'products': [], 'error': str(e)}

    return _search_by_base64(image_base64, access_token, app_key, app_secret, page, size)


def _search_by_base64(image_base64: str, access_token: str,
                      app_key: str = None, app_secret: str = None,
                      page: int = 1, size: int = 20) -> Dict[str, Any]:
    """Base64 图片数据 → 按图搜索"""
    try:
        resp = search_product_by_image(
            image_base64, access_token,
            page=page, size=size,
            app_key=app_key, app_secret=app_secret,
        )
    except Exception as e:
        logger.error(f"按图搜索 API 调用失败: {e}")
        return {'success': False, 'products': [], 'error': str(e)}

    if 'error' in resp:
        logger.warning(f"按图搜索返回错误: {resp.get('error_message', resp['error'])}")
        return {'success': False, 'products': [], 'error': resp.get('error_message', resp['error'])}

    # 解析结果
    products = _parse_image_search_result(resp)
    return {'success': True, 'products': products, 'error': None}


def _parse_image_search_result(api_response: dict) -> list:
    """解析按图搜索 API 响应

    1688 按图搜索返回结构（按实际字段调整）：
    {'result': {'productList': [{'productID':..., 'subject':..., ...}]}}
    """
    result_node = api_response.get('result', {})
    if isinstance(result_node, dict):
        product_list = (result_node.get('productList')
                        or result_node.get('products')
                        or result_node.get('items')
                        or [])
    elif isinstance(result_node, list):
        product_list = result_node
    else:
        product_list = []

    products = []
    for item in product_list:
        if not isinstance(item, dict):
            continue
        product = {
            'product_id': item.get('productID', item.get('id', '')),
            'title': item.get('subject', item.get('title', '')),
            'price': float(item.get('price', 0)),
            'original_price': float(item.get('originalPrice', 0)),
            'image_url': item.get('imageUrl', item.get('imgUrl', '')),
            'source_url': item.get('detailUrl', item.get('url', '')),
            'seller_name': item.get('companyName', item.get('sellerName', '')),
            'location': item.get('province', item.get('location', '')),
            'moq': int(item.get('minOrderQuantity', 1)),
        }
        products.append(product)

    return products


if __name__ == '__main__':
    from ..plugin_i18n import t as _
    print(_("1688 Image Search Service"))
    print(_("Usage: search_by_image_file(path, token)"))
    print("      search_by_image_url(url, token)")
