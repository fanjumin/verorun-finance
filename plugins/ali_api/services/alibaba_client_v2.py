#!/usr/bin/env python3
"""
1688 新版开放平台 API 客户端
对应文档：  alibaba.product.get-1        → 获取商品详情
  alibaba.category.searchSPUInfo-1  → 搜索SPU信息

新版 API 与旧版的区别：
  1. API 路径：param2/1/com.alibaba.product/   （不是 cn.alibaba.open）
  2. 参数名：_aop_signature, _aop_timestamp     （不是 sign, timestamp）
  3. 必须传 access_token（OAuth 2.0 授权）
  4. 签名方式：access_token + 排序参数 + access_token
"""

import json, time, hmac, hashlib, base64, urllib.request, urllib.parse, os, sys

API_GATEWAY = "https://gw.open.1688.com/openapi"


def generate_signature(params: dict, app_secret: str, access_token: str) -> str:
    """生成新版 1688 API 签名
    
    签名算法：
      1. 除去 _aop_signature 外，按 key 字母排序
      2. key1value1key2value2... 拼接
      3. access_token + 拼接串 + access_token
      4. HMAC-SHA1(app_secret) → Base64
    """
    sorted_params = sorted((k, v) for k, v in params.items() if k != '_aop_signature')
    string_to_sign = ''.join(f'{k}{v}' for k, v in sorted_params)
    string_to_sign = access_token + string_to_sign + access_token
    sig = hmac.new(
        app_secret.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha1
    ).digest()
    return base64.b64encode(sig).decode('utf-8')


def call_api(api_name: str, api_version: str, app_key: str, app_secret: str,
             access_token: str, biz_params: dict = None) -> dict:
    """调用 1688 新版 API
    
    Args:
        api_name: 如 'alibaba.product.get-1'
        api_version: 如 '1' 
        app_key: AppKey
        app_secret: AppSecret
        access_token: OAuth access_token
        biz_params: 业务参数（如 productID）
    Returns:
        API 响应 dict
    """
    # 解析命名空间
    # alibaba.product.get-1 → namespace=com.alibaba.product, api=alibaba.product.get-1
    parts = api_name.split(':')
    if len(parts) == 2:
        namespace = parts[0]
        api_method = parts[1]
    else:
        api_method = api_name
        namespace = api_method.rsplit('.', 2)[0]
        if not namespace.startswith('com.'):
            namespace = f'com.{namespace}'
    
    # 构建请求参数（必须包含 _aop_timestamp）
    params = {
        '_aop_timestamp': str(int(time.time() * 1000)),
        'access_token': access_token,
    }
    if biz_params:
        params.update(biz_params)
    
    # 生成签名
    signature = generate_signature(params, app_secret, access_token)
    params['_aop_signature'] = signature
    
    # 构建 URL
    url = f'{API_GATEWAY}/param2/{api_version}/{namespace}/{api_method}'
    
    # POST 请求
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            result = json.loads(body)
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return {'error': f'HTTP {e.code}', 'error_message': body}
    except Exception as e:
        return {'error': str(e)}


def get_access_token(app_key: str, app_secret: str, code: str, redirect_uri: str) -> dict:
    """用授权码换取 access_token
    
    Args:
        app_key: AppKey
        app_secret: AppSecret
        code: OAuth 授权码
        redirect_uri: 回调地址
    
    Returns:
        Token 信息
    """
    url = f'{API_GATEWAY}/http/1/system.oauth2/getToken/{app_key}'
    params = {
        'grant_type': 'authorization_code',
        'code': code,
        'need_refresh_token': 'true',
        'client_id': app_key,
        'client_secret': app_secret,
        'redirect_uri': redirect_uri,
    }
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return {'error': f'HTTP {e.code}', 'error_message': body}


def refresh_access_token(app_key: str, app_secret: str, refresh_token: str) -> dict:
    """刷新 access_token"""
    url = f'{API_GATEWAY}/http/1/system.oauth2/getToken/{app_key}'
    params = {
        'grant_type': 'refresh_token',
        'client_id': app_key,
        'client_secret': app_secret,
        'refresh_token': refresh_token,
    }
    data = urllib.parse.urlencode(params).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return {'error': f'HTTP {e.code}', 'error_message': body}


def get_auth_url(app_key: str, redirect_uri: str, state: str = '') -> str:
    """获取 1688 OAuth 授权 URL
    
    site=1688 是必填参数，标识授权站点
    文档：https://open.1688.com/doc/apiAuth.htm
    """
    params = {
        'response_type': 'code',
        'client_id': app_key,
        'redirect_uri': redirect_uri,
        'site': '1688',
        'state': state or 'ali_connect',
    }
    return f"https://auth.1688.com/oauth/authorize?{urllib.parse.urlencode(params)}"


# ===== 快捷方法 =====

def get_product(product_id: str, access_token: str,
                app_key: str = None, app_secret: str = None,
                web_site: str = '1688') -> dict:
    """获取 1688 商品详情"""
    if not app_key:
        from ..config import config
        app_key = config['alibaba']['app_key']
        app_secret = config['alibaba']['app_secret']
    
    return call_api(
        'com.alibaba.product:alibaba.product.get-1',
        '1', app_key, app_secret, access_token,
        {'productID': product_id, 'webSite': web_site}
    )


def search_spu(category_id: int, page: int, size: int,
               access_token: str,
               app_key: str = None, app_secret: str = None) -> dict:
    """按类目搜索 SPU"""
    if not app_key:
        from ..config import config
        app_key = config['alibaba']['app_key']
        app_secret = config['alibaba']['app_secret']
    
    return call_api(
        'com.alibaba.product:alibaba.category.searchSPUInfo-1',
        '1', app_key, app_secret, access_token,
        {'categoryId': category_id, 'index': page, 'size': size}
    )


def get_product_reviews(product_id: str, access_token: str,
                        page: int = 1, size: int = 20,
                        app_key: str = None, app_secret: str = None) -> dict:
    """获取 1688 商品评论/评价

    API: alibaba.product.review.get-1
    """
    if not app_key:
        from ..config import config
        app_key = config['alibaba']['app_key']
        app_secret = config['alibaba']['app_secret']
    return call_api(
        'com.alibaba.product:alibaba.product.review.get-1',
        '1', app_key, app_secret, access_token,
        {'productId': product_id, 'page': page, 'pageSize': size}
    )


def search_product_by_image(image_base64: str, access_token: str,
                            page: int = 1, size: int = 20,
                            app_key: str = None, app_secret: str = None) -> dict:
    """按图搜索 1688 商品

    API: alibaba.product.search.img-1
    """
    if not app_key:
        from ..config import config
        app_key = config['alibaba']['app_key']
        app_secret = config['alibaba']['app_secret']
    return call_api(
        'com.alibaba.product:alibaba.product.search.img-1',
        '1', app_key, app_secret, access_token,
        {'imageBase64': image_base64, 'page': page, 'pageSize': size}
    )


def get_store_products(seller_member_id: str, access_token: str,
                       page: int = 1, size: int = 20,
                       app_key: str = None, app_secret: str = None) -> dict:
    """获取店铺全量商品

    API: alibaba.store.item.list.get-1
    """
    if not app_key:
        from ..config import config
        app_key = config['alibaba']['app_key']
        app_secret = config['alibaba']['app_secret']
    return call_api(
        'com.alibaba.product:alibaba.store.item.list.get-1',
        '1', app_key, app_secret, access_token,
        {'sellerMemberId': seller_member_id, 'page': page, 'pageSize': size}
    )


# ===== 命令行测试 =====
if __name__ == '__main__':
    from ..plugin_i18n import t as _
    import pprint
    
    APP_KEY = os.environ.get('ALIBABA_APP_KEY', '')
    APP_SECRET = os.environ.get('ALIBABA_APP_SECRET', '')
    
    if not APP_KEY:
        print('⚠ 请设置 ALIBABA_APP_KEY 和 ALIBABA_APP_SECRET')
        sys.exit(1)
    
    print(f'AppKey: {APP_KEY[:8]}...')
    print(f'API Gateway: {API_GATEWAY}')
    
    # 测试授权 URL 生成
    auth_url = get_auth_url(APP_KEY, 'https://your-domain.com/ali-callback', 'test')
    print(f'\n授权 URL:\n{auth_url}')
    
    # 测试签名生成
    test_params = {'_aop_timestamp': '1625000000000', 'access_token': 'test_token', 'productID': '123'}
    sig = generate_signature(test_params, APP_SECRET, 'test_token')
    print(f'\n签名测试: {sig[:30]}...')
    
    print('\n▶ 需要先通过授权 URL 获取 code，然后调用 get_access_token()')
    print(_('    Call get_product() to query product after obtaining access_token'))
