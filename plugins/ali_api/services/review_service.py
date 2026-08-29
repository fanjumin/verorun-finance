#!/usr/bin/env python3
"""
1688 商品评论采集服务

功能：1. 获取商品评论/评价列表
2. 评论统计（好评率/中评率/差评率）
3. 评论存储到 ali_api_reviews 表
4. 支持分页拉取
"""

import logging
from typing import Dict, Any, Optional, Tuple

from ..models import get_db, AliApiReview, AliApiItem
from .alibaba_client_v2 import get_product_reviews

logger = logging.getLogger(__name__)


def fetch_and_store_reviews(product_id: str, access_token: str,
                            app_key: str = None, app_secret: str = None,
                            max_pages: int = 5) -> Dict[str, Any]:
    """获取 1688 商品评论并存入数据库

    Args:
        product_id: 1688 商品 ID
        access_token: OAuth access_token
        app_key: AppKey（默认从 config 读取）
        app_secret: AppSecret（默认从 config 读取）
        max_pages: 最大拉取页数（每页 20 条）

    Returns:
        {'total_fetched': int, 'total_stored': int,
         'pages_fetched': int, 'error': str|None}
    """
    result = {
        'total_fetched': 0,
        'total_stored': 0,
        'pages_fetched': 0,
        'error': None,
    }

    for page in range(1, max_pages + 1):
        try:
            resp = get_product_reviews(
                product_id, access_token,
                page=page, size=20,
                app_key=app_key, app_secret=app_secret
            )
        except Exception as e:
            logger.error(f"获取评论失败 (page {page}): {e}")
            result['error'] = str(e)
            break

        # 解析返回
        if 'error' in resp:
            logger.warning(f"API 返回错误 (page {page}): {resp.get('error_message', resp['error'])}")
            result['error'] = resp.get('error_message', resp['error'])
            break

        # 提取评论列表
        reviews = _extract_reviews(resp)
        if not reviews:
            break  # 无更多评论

        result['total_fetched'] += len(reviews)

        # 存库
        with get_db() as conn:
            stored = AliApiReview.batch_insert(conn, product_id, reviews)
            result['total_stored'] += stored

        result['pages_fetched'] = page

        # 如果返回数 < 20，说明已到最后一页
        if len(reviews) < 20:
            break

    return result


def _extract_reviews(api_response: dict) -> list:
    """从 API 响应中提取评论列表

    1688 评价 API 返回结构（仅供参考，按实际字段调整）：
    {'result': {'evaluateList': [{'id':..., 'buyerName':..., ...}]}}
    """
    result_node = api_response.get('result', {})
    if isinstance(result_node, dict):
        evaluate_list = (result_node.get('evaluateList')
                         or result_node.get('evaluations')
                         or result_node.get('reviews')
                         or [])
    elif isinstance(result_node, list):
        evaluate_list = result_node
    else:
        evaluate_list = []

    reviews = []
    for item in evaluate_list:
        if not isinstance(item, dict):
            continue
        review = {
            'review_id': str(item.get('id', item.get('evaluateId', item.get('reviewId', '')))),
            'buyer_name': item.get('buyerName', item.get('buyerMemberId', item.get('userName', ''))),
            'rating': int(item.get('rating', item.get('starLevel', item.get('score', 5)))),
            'content': item.get('content', item.get('feedback', item.get('evaluateContent', ''))),
            'review_time': item.get('gmtCreate', item.get('createTime', item.get('reviewTime', ''))),
            'spec_info': item.get('skuInfo', item.get('specInfo', '')),
            'images': item.get('imageList', item.get('images', [])),
            'is_anonymous': bool(item.get('isAnonymous', item.get('anonymous', False))),
            'reply_content': item.get('sellerReply', item.get('reply', item.get('replyContent', ''))),
            'reply_time': item.get('replyTime', item.get('gmtReply', '')),
            'raw_data': item,
        }
        reviews.append(review)

    return reviews


def get_review_stats(product_id: str) -> dict:
    """获取商品评论统计"""
    with get_db() as conn:
        return AliApiReview.get_stats(conn, product_id)


def get_reviews(product_id: str, page: int = 1, page_size: int = 20) -> list:
    """获取商品评论（分页）"""
    offset = (page - 1) * page_size
    with get_db() as conn:
        return AliApiReview.get_by_product(conn, product_id, limit=page_size, offset=offset)


if __name__ == '__main__':
    import pprint
    from ..plugin_i18n import t as _
    # 测试：仅展示接口
    print(_("1688 Product Review Collection Service"))
    print(_("Usage: fetch_and_store_reviews(product_id, access_token)"))
    print("      get_review_stats(product_id)")
    print("      get_reviews(product_id, page)")
