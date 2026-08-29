#!/usr/bin/env python3
"""
阿里巴巴开放平台 API 客户端
功能：1. HMAC-SHA1 签名生成
2. API 请求封装
3. 错误处理与重试
4. 响应数据解析
"""

import hashlib
import hmac
import json
import time
import random
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
import logging

from ..config import config

logger = logging.getLogger(__name__)

# User-Agent 池：随机化请求头，降低反爬识别
_USER_AGENTS = [
    # Chrome Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    # Chrome macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    # Edge
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
    # Firefox
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0',
    # Safari macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
]


def _random_ua() -> str:
    """随机返回一个 User-Agent"""
    return random.choice(_USER_AGENTS)

class AlibabaClient:
    """阿里巴巴开放平台客户端"""
    
    def __init__(self, app_key: str = None, app_secret: str = None):
        """初始化客户端"""
        self.app_key = app_key or config['alibaba']['app_key']
        self.app_secret = app_secret or config['alibaba']['app_secret']
        self.api_gateway = config['alibaba']['api_gateway']
        self.api_version = config['alibaba']['api_version']
        self.sign_method = config['alibaba']['sign_method']
        
        if not self.app_key or not self.app_secret:
            raise ValueError("阿里巴巴 AppKey 和 AppSecret 必须配置")
    
    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """生成 HMAC-SHA1 签名"""
        # 1. 参数排序
        sorted_params = sorted(params.items())
        
        # 2. 构建待签名字符串
        string_to_sign = ''
        for key, value in sorted_params:
            if value is not None:
                string_to_sign += f'{key}{value}'
        
        # 3. 添加 AppSecret
        string_to_sign = self.app_secret + string_to_sign + self.app_secret
        
        # 4. HMAC-SHA1 签名
        if self.sign_method == 'hmac-sha1':
            signature = hmac.new(
                self.app_secret.encode('utf-8'),
                string_to_sign.encode('utf-8'),
                hashlib.sha1
            ).digest()
            
            # 5. Base64 编码
            import base64
            signature = base64.b64encode(signature).decode('utf-8')
        else:
            raise ValueError(f"不支持的签名方法: {self.sign_method}")
        
        return signature
    
    def _prepare_params(self, api_name: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """准备请求参数"""
        if params is None:
            params = {}
        
        # 基础参数
        base_params = {
            'app_key': self.app_key,
            'timestamp': datetime.now().strftime(config['alibaba']['timestamp_format']),
            'format': 'json',
            'v': '2.0',
            'sign_method': self.sign_method,
            'method': api_name,
        }
        
        # 合并参数
        all_params = {**base_params, **params}
        
        # 移除空值
        all_params = {k: v for k, v in all_params.items() if v is not None}
        
        # 生成签名
        all_params['sign'] = self._generate_signature(all_params)
        
        return all_params
    
    def _make_request(self, api_name: str, params: Dict[str, Any] = None, 
                     max_retries: int = None) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """发送API请求"""
        if max_retries is None:
            max_retries = config['rate_limit']['max_retries']
        
        # 准备参数
        request_params = self._prepare_params(api_name, params)
        
        # 构建URL
        url = f"{self.api_gateway}/{self.api_version}{api_name}"
        
        # 编码参数
        encoded_params = urllib.parse.urlencode(request_params)
        full_url = f"{url}?{encoded_params}"
        
        # 重试逻辑
        last_error = None
        for attempt in range(max_retries):
            try:
                start_time = time.time()
                
                # 发送请求
                req = urllib.request.Request(full_url)
                req.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
                req.add_header('User-Agent', _random_ua())
                req.add_header('Accept', 'application/json')
                req.add_header('Accept-Language', 'zh-CN,zh;q=0.9')
                
                with urllib.request.urlopen(req, timeout=10) as response:
                    response_data = response.read().decode('utf-8')
                    response_time = int((time.time() - start_time) * 1000)  # 毫秒
                    
                    # 解析响应
                    result = json.loads(response_data)
                    
                    # 检查错误
                    if 'error' in result:
                        error_msg = result.get('error_message', '未知错误')
                        error_code = result.get('error_code', 'UNKNOWN')
                        logger.error(f"阿里巴巴API错误: {error_code} - {error_msg}")
                        return False, result, error_msg
                    
                    # 成功响应
                    logger.info(f"阿里巴巴API调用成功: {api_name}, 耗时: {response_time}ms")
                    return True, result, None
                    
            except urllib.error.HTTPError as e:
                last_error = f"HTTP错误: {e.code} - {e.reason}"
                logger.error(f"阿里巴巴API HTTP错误 (尝试 {attempt+1}/{max_retries}): {last_error}")
            except urllib.error.URLError as e:
                last_error = f"URL错误: {e.reason}"
                logger.error(f"阿里巴巴API URL错误 (尝试 {attempt+1}/{max_retries}): {last_error}")
            except json.JSONDecodeError as e:
                last_error = f"JSON解析错误: {e}"
                logger.error(f"阿里巴巴API JSON解析错误 (尝试 {attempt+1}/{max_retries}): {last_error}")
            except Exception as e:
                last_error = f"未知错误: {e}"
                logger.error(f"阿里巴巴API未知错误 (尝试 {attempt+1}/{max_retries}): {last_error}")
            
            # 重试延迟：指数退避 + 抖动 + 上限（行业标准，避免请求风暴）
            if attempt < max_retries - 1:
                base_delay = config['rate_limit']['retry_delay'] * (2 ** attempt)
                delay = min(base_delay, 10.0) * (0.8 + 0.4 * random.random())
                time.sleep(delay)
        
        # 所有重试都失败
        logger.error(f"阿里巴巴API调用失败: {api_name}, 错误: {last_error}")
        return False, {}, last_error
    
    # ===== 具体API方法 =====
    
    def get_product(self, product_id: str, fields: str = None) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        获取商品详情
        API: alibaba.product.get
        """
        params = {
            'productID': product_id,
        }
        if fields:
            params['fields'] = fields
        
        return self._make_request('alibaba.product.get', params)
    
    def search_products(self, keywords: str, page_no: int = 1, page_size: int = 20,
                       category_id: str = None, order_by: str = None) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        搜索商品
        API: alibaba.product.search
        """
        params = {
            'keywords': keywords,
            'pageNo': page_no,
            'pageSize': page_size,
        }
        
        if category_id:
            params['categoryId'] = category_id
        if order_by:
            params['orderBy'] = order_by
        
        return self._make_request('alibaba.product.search', params)
    
    def get_category(self, category_id: str = None, parent_id: str = None,
                    level: int = None) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        获取类目信息
        API: alibaba.category.get
        """
        params = {}
        if category_id:
            params['categoryID'] = category_id
        if parent_id:
            params['parentID'] = parent_id
        if level is not None:
            params['level'] = level
        
        return self._make_request('alibaba.category.get', params)
    
    def get_logistics(self, order_id: str = None, logistics_id: str = None) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        获取物流信息
        API: alibaba.logistics.get
        """
        params = {}
        if order_id:
            params['orderID'] = order_id
        if logistics_id:
            params['logisticsID'] = logistics_id
        
        return self._make_request('alibaba.logistics.get', params)
    
    def parse_product_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """解析商品API响应为标准格式"""
        if 'result' not in response:
            return {}
        
        result = response['result']
        product = result.get('product', {})
        
        # 解析 B2B 属性：阶梯批发价
        wholesale_prices = []
        price_items = product.get('priceRanges', []) or product.get('priceRangeList', [])
        if isinstance(price_items, list):
            for pr in price_items:
                wholesale_prices.append({
                    'min_quantity': pr.get('startQuantity', pr.get('minOrderQuantity', 0)),
                    'price': float(pr.get('price', 0)),
                })
        
        # 解析卖家信用
        seller_info = product.get('sellerInfo', {}) or {}
        seller_credit_raw = seller_info.get('creditLevel', 0) or product.get('creditLevel', 0)
        try:
            seller_credit = int(seller_credit_raw)
        except (ValueError, TypeError):
            seller_credit = 0
        shop_level_raw = seller_info.get('shopLevel', 0) or product.get('shopLevel', 0)
        try:
            shop_level = int(shop_level_raw)
        except (ValueError, TypeError):
            shop_level = 0
        
        # 是否支持一件代发
        support_agent = product.get('supportAgent', 0) or product.get('isSupportAgent', 0)
        if isinstance(support_agent, str):
            support_agent = 1 if support_agent.lower() in ('true', 'yes', '1', 'y') else 0
        
        # 提取商品信息
        parsed = {
            'product_id': product.get('productID', ''),
            'title': product.get('subject', ''),
            'description': product.get('description', ''),
            'price': float(product.get('price', 0)),
            'original_price': float(product.get('originalPrice', 0)),
            'currency': product.get('currencyCode', 'CNY'),
            'category': product.get('categoryName', ''),
            'category_id': product.get('categoryID', ''),
            'images': product.get('imageList', []),
            'specs': product.get('productFeatureList', {}),
            'source_url': product.get('detailUrl', ''),
            'seller_id': product.get('sellerMemberId', ''),
            'seller_name': product.get('companyName', ''),
            'seller_credit': seller_credit,
            'shop_level': shop_level,
            'location': product.get('province', ''),
            'unit': product.get('unit', ''),
            'min_order_quantity': product.get('minOrderQuantity', 1),
            'moq': product.get('minOrderQuantity', 1),
            'wholesale_price': wholesale_prices,
            'is_support_agent': support_agent,
            'package_size': product.get('packageSize', ''),
            'weight': product.get('weight', ''),
            'volume': product.get('volume', ''),
            'status': product.get('productStatus', ''),
            'api_response': response,
        }
        
        return parsed
    
    def parse_search_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """解析搜索API响应为标准格式"""
        if 'result' not in response:
            return {'products': [], 'total': 0, 'page_no': 1, 'page_size': 20}
        
        result = response['result']
        products = result.get('products', [])
        
        parsed_products = []
        for product in products:
            # 搜索列表里也有简明 B2B 字段
            wholesale_prices = []
            price_items = product.get('priceRanges', []) or []
            if isinstance(price_items, list):
                for pr in price_items:
                    wholesale_prices.append({
                        'min_quantity': pr.get('startQuantity', pr.get('minOrderQuantity', 0)),
                        'price': float(pr.get('price', 0)),
                    })
            support_agent = product.get('supportAgent', 0) or product.get('isSupportAgent', 0)
            if isinstance(support_agent, str):
                support_agent = 1 if support_agent.lower() in ('true', 'yes', '1', 'y') else 0
            parsed = {
                'product_id': product.get('productID', ''),
                'title': product.get('subject', ''),
                'price': float(product.get('price', 0)),
                'original_price': float(product.get('originalPrice', 0)),
                'currency': product.get('currencyCode', 'CNY'),
                'category': product.get('categoryName', ''),
                'image_url': product.get('imageUrl', ''),
                'source_url': product.get('detailUrl', ''),
                'seller_name': product.get('companyName', ''),
                'seller_credit': product.get('creditLevel', 0),
                'location': product.get('province', ''),
                'min_order_quantity': product.get('minOrderQuantity', 1),
                'moq': product.get('minOrderQuantity', 1),
                'wholesale_price': wholesale_prices,
                'is_support_agent': support_agent,
                'status': product.get('productStatus', ''),
            }
            parsed_products.append(parsed)
        
        return {
            'products': parsed_products,
            'total': result.get('totalResults', 0),
            'page_no': result.get('pageNo', 1),
            'page_size': result.get('pageSize', 20),
            'total_pages': result.get('totalPages', 1),
        }

    # ===== 店铺全量商品 =====

    def search_store_products(self, seller_id: str, page_no: int = 1, page_size: int = 20,
                              order_by: str = None) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        获取店铺全量商品
        API: alibaba.store.item.list.get
        """
        params = {
            'sellerMemberId': seller_id,
            'pageNo': page_no,
            'pageSize': page_size,
        }
        if order_by:
            params['orderBy'] = order_by
        return self._make_request('alibaba.store.item.list.get', params)

# 单例客户端实例
_client_instance = None

def get_client() -> AlibabaClient:
    """获取阿里巴巴客户端单例"""
    global _client_instance
    if _client_instance is None:
        _client_instance = AlibabaClient()
    return _client_instance

if __name__ == "__main__":
    # 测试客户端
    import pprint
    
    client = AlibabaClient()
    print("阿里巴巴客户端初始化成功")
    
    # 测试配置
    print(f"AppKey: {client.app_key[:8]}...")
    print(f"API网关: {client.api_gateway}")
    
    # 测试签名生成
    test_params = {'test': 'value', 'app_key': 'test_key'}
    try:
        signature = client._generate_signature(test_params)
        print(f"签名测试: {signature[:20]}...")
    except Exception as e:
        print(f"签名测试失败: {e}")
