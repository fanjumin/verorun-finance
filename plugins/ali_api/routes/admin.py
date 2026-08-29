#!/usr/bin/env python3
"""
阿里巴巴API管理界面路由


功能：
1. 商品采集界面
2. 缓存管理
3. 统计查看
4. 风控配置
"""

import json
import logging
import os
import sys
import functools
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from flask import Blueprint, request, jsonify, render_template, make_response, current_app, send_from_directory, redirect
from werkzeug.utils import secure_filename

from ..config import config
from ..services.alibaba_client import get_client
from ..services.alibaba_client_v2 import (
    get_access_token, refresh_access_token, get_auth_url,
    get_product as get_product_v2, call_api,
    generate_signature,
)
from ..services.rate_limiter import get_rate_limit_manager
from ..services.cache_service import get_cache_service
from ..services.ai_processor import get_ai_processor, is_ai_available, get_ai_status
from ..models import get_db, AliApiItem, AliApiLog, AliApiUserStats
from i18n import _

# 创建蓝图
ali_admin_bp = Blueprint('ali_api_admin', __name__, url_prefix='/admin/ali-api',
                          template_folder='../templates',
                          static_folder='../static',
                          static_url_path='static')

logger = logging.getLogger(__name__)

# 模块加载时一次性将 auth-center 加入 sys.path（避免每个请求都重复 insert）
_AUTH_CENTER_PATH = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'auth-center')
if _AUTH_CENTER_PATH not in sys.path:
    sys.path.insert(0, _AUTH_CENTER_PATH)

# ===== 辅助函数 =====

import secrets

def _generate_csrf_token():
    """生成 CSRF Token"""
    return secrets.token_urlsafe(32)

def _get_csrf_token():
    """从请求中获取 CSRF Token（优先 Header，其次 Cookie）"""
    token = request.headers.get('X-CSRF-Token', '')
    if not token:
        token = request.cookies.get('csrf_token', '')
    return token

def _validate_csrf():
    """验证 CSRF Token（双重提交 Cookie 模式）"""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    # 从 Cookie 获取原始 token
    cookie_token = request.cookies.get('csrf_token', '')
    # 从 Header 获取提交的 token
    header_token = _get_csrf_token()
    if not cookie_token or not header_token:
        return False
    return secrets.compare_digest(cookie_token, header_token)

def _require_admin():
    """检查管理员权限 — 集成 JWT SSO"""
    auth = request.headers.get('Authorization', '')
    token = None
    if auth and auth.startswith('Bearer '):
        token = auth[7:]
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    if not token:
        token = request.args.get('token')  # 支持 URL 传 token（iframe 加载场景）
    if not token:
        return None
    
    from services.jwt_service import validate_token
    payload = validate_token(token)
    if not payload or not payload.get('is_admin'):
        return None
    return {'user_id': payload['user_id'], 'is_admin': True}

def _require_admin_or_error():
    """检查管理员权限，失败则返回 401 错误响应"""
    admin = _require_admin()
    if not admin:
        return None, _error(_('Login required or insufficient permissions'), 401)
    return admin, None


def csrf_protect(f):
    """CSRF 保护装饰器"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _validate_csrf():
            return _error(_('CSRF verification failed, please refresh and retry'), 403)
        return f(*args, **kwargs)
    return wrapper

def _success(data=None, message=None):
    """成功响应"""
    message = message or _('Operation succeeded')
    resp = jsonify({'success': True, 'data': data, 'message': message})
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    return resp

def _error(message=None, code=400):
    """错误响应"""
    message = message or _('Operation failed')
    resp = jsonify({'success': False, 'error': message}), code
    if isinstance(resp, tuple):
        resp[0].headers['X-Content-Type-Options'] = 'nosniff'
        resp[0].headers['X-Frame-Options'] = 'DENY'
    return resp

def _get_pagination_params():
    """获取分页参数"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    return max(1, page), min(max(1, per_page), 100)

# ===== 图片上传配置 =====
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'platform', 'static', 'products')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB

def _ensure_upload_dir():
    """确保上传目录存在"""
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ===== 静态文件服务（上传的图片） =====
@ali_admin_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """提供上传的图片文件"""
    return send_from_directory(UPLOAD_FOLDER, filename)

# ===== 路由定义 =====

@ali_admin_bp.route('/csrf-token', methods=['GET'])
def csrf_token_endpoint():
    """签发 CSRF Token（内联管理页使用，双提交 Cookie 模式）
    插件标准 v1.5 §10.3/§12.11：插件走内联通道时由该端点签发 token 并种 cookie，
    前端所有 POST/DELETE 携带 X-CSRF-Token 头。"""
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)
    token = _generate_csrf_token()
    _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
    resp = jsonify({'success': True, 'token': token})
    resp.set_cookie('csrf_token', token,
                    max_age=3600, httponly=False, samesite='Lax', secure=_is_https)
    return resp


@ali_admin_bp.route('/')
def index():
    """管理界面首页（未登录跳转登录页）
    注：菜单已走内联通道（l_ali_api），本 iframe 页面保留为旧直连入口。"""
    admin = _require_admin()
    if not admin:
        return redirect('/admin/login')
    csrf_token = _generate_csrf_token()
    response = make_response(render_template('ali_admin/index.html', csrf_token=csrf_token))
    # 设置 CSRF Cookie（HttpOnly=False 以让 JS 读取，必须 SameSite=Lax）
    _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
    response.set_cookie('csrf_token', csrf_token, 
                        max_age=3600, httponly=False, samesite='Lax', secure=_is_https)
    # 添加安全响应头
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response

@ali_admin_bp.route('/dashboard')
def dashboard():
    """仪表盘数据"""
    admin, err = _require_admin_or_error()
    if err:
        return err
    try:
        # 获取统计信息
        with get_db() as conn:
            # 商品统计
            total_items = conn.execute('SELECT COUNT(*) as count FROM ali_api_items').fetchone()['count']
            active_items = conn.execute("SELECT COUNT(*) as count FROM ali_api_items WHERE status = 'active'").fetchone()['count']
            
            # API调用统计
            total_calls = conn.execute('SELECT COUNT(*) as count FROM ali_api_logs').fetchone()['count']
            today_calls = conn.execute("""
                SELECT COUNT(*) as count FROM ali_api_logs 
                WHERE created_at::date = CURRENT_DATE
            """).fetchone()['count']
            
            # 用户统计
            total_users = conn.execute('SELECT COUNT(*) as count FROM ali_api_user_stats').fetchone()['count']
        
        # 风控统计
        try:
            rate_limit_stats = get_rate_limit_manager().get_stats()
        except Exception as e:
            rate_limit_stats = {'error': str(e), 'status': 'degraded'}
        
        # 缓存统计
        cache_stats = get_cache_service().stats()
        
        # AI可用性
        ai_available = is_ai_available()
        
        return _success({
            'items': {
                'total': total_items,
                'active': active_items,
            },
            'api_calls': {
                'total': total_calls,
                'today': today_calls,
            },
            'users': {
                'total': total_users,
            },
            'rate_limit': rate_limit_stats,
            'cache': cache_stats,
            'ai': get_ai_status(),
            'config': {
                'app_key': config['alibaba']['app_key'][:8] + '...' if config['alibaba']['app_key'] else '未配置',
                'api_gateway': config['alibaba']['api_gateway'],
            }
        })
        
    except Exception as e:
        logger.error(f"获取仪表盘数据失败: {e}")
        return _error(_('Failed to fetch data: {error}', error=str(e)), 500)

@ali_admin_bp.route('/items')
def list_items():
    """列出商品"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        page, per_page = _get_pagination_params()
        status = request.args.get('status', 'active')
        keyword = request.args.get('keyword', '')
        
        offset = (page - 1) * per_page
        
        with get_db() as conn:
            if keyword:
                # 搜索商品
                items = AliApiItem.search_items(conn, keyword, limit=per_page)
                total = len(items)
            else:
                # 分页查询
                items = AliApiItem.list_items(conn, status, per_page, offset)
                total = conn.execute('SELECT COUNT(*) as count FROM ali_api_items WHERE status = %s', (status,)).fetchone()['count']
            
            # 格式化数据
            formatted_items = []
            for item in items:
                # 解析JSON字段
                if isinstance(item.get('images'), str):
                    try:
                        item['images'] = json.loads(item['images'])
                    except:
                        item['images'] = []
                
                if isinstance(item.get('specs'), str):
                    try:
                        item['specs'] = json.loads(item['specs'])
                    except:
                        item['specs'] = {}
                
                if isinstance(item.get('api_response'), str):
                    try:
                        item['api_response'] = json.loads(item['api_response'])
                    except:
                        item['api_response'] = {}
                
                formatted_items.append(item)
        
        return _success({
            'items': formatted_items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page,
            }
        })
        
    except Exception as e:
        logger.error(f"列出商品失败: {e}")
        return _error(_('Failed to list products: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>')
def get_item(item_id):
    """获取商品详情"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            
            if not item:
                return _error(_('Product not found'), 404)
            
            # 解析JSON字段
            for field in ['images', 'specs', 'api_response']:
                if isinstance(item.get(field), str):
                    try:
                        item[field] = json.loads(item[field])
                    except:
                        item[field] = {} if field in ['specs', 'api_response'] else []
            
            return _success(item)
            
    except Exception as e:
        logger.error(f"获取商品详情失败: {e}")
        return _error(_('Failed to fetch product details: {error}', error=str(e)))

@ali_admin_bp.route('/items/collect', methods=['POST'])
@csrf_protect
def collect_product():
    """采集商品"""
    try:
        data = request.json
        if not data:
            return _error(_('Request data cannot be empty'))
        
        product_id = data.get('product_id')
        if not product_id:
            return _error(_('Product ID cannot be empty'))
        
        # 检查权限
        admin_info, err = _require_admin_or_error()
        if not admin_info:
            return err
        user_id = admin_info['user_id']
        
        # 检查风控限制
        rate_manager = get_rate_limit_manager()
        allowed, reason = rate_manager.check_all_limits(user_id, 'alibaba.product.get')
        if not allowed:
            return _error(_('Rate limit exceeded: {reason}', reason=reason), 429)
        
        _permit_released = [False]
        
        def _safe_release_permit():
            if not _permit_released[0]:
                rate_manager.concurrent_controller.release_safe()
                _permit_released[0] = True
        
        try:
            # 检查缓存
            cache_service = get_cache_service()
            found, cached_product = cache_service.get_product(product_id)
            
            if found:
                logger.info(f"从缓存获取商品 {product_id}")
                
                # 保存到数据库
                with get_db() as conn:
                    item_id = AliApiItem.insert_or_update(conn, cached_product)
                    conn.commit()
                
                # 记录API日志（缓存命中）
                with get_db() as conn:
                    AliApiLog.log_request(conn, {
                        'user_id': user_id,
                        'endpoint': 'alibaba.product.get',
                        'params': {'productID': product_id},
                        'response_code': 200,
                        'response_time': 0,
                        'success': True,
                        'error_msg': '缓存命中',
                        'ip_address': request.remote_addr,
                    })
                    conn.commit()
                
                rate_manager.record_api_result('alibaba.product.get', True, 0)
                _permit_released[0] = True
                
                return _success({
                    'item_id': item_id,
                    'from_cache': True,
                    'product': cached_product,
                })
            
            # 调用阿里巴巴API
            client = get_client()
            start_time = datetime.now()
            
            success, response, error_msg = client.get_product(product_id)
            
            response_time = (datetime.now() - start_time).total_seconds() * 1000
            
            # 记录API结果（内部释放并发许可）
            rate_manager.record_api_result('alibaba.product.get', success, int(response_time))
            _permit_released[0] = True
            
            if not success:
                # 记录失败日志
                with get_db() as conn:
                    AliApiLog.log_request(conn, {
                        'user_id': user_id,
                        'endpoint': 'alibaba.product.get',
                        'params': {'productID': product_id},
                        'response_code': 500,
                        'response_time': int(response_time),
                        'success': False,
                        'error_msg': error_msg,
                        'ip_address': request.remote_addr,
                    })
                    conn.commit()
                
                return _error(_('API call failed: {error}', error=error_msg))
            
            # 解析响应
            product_data = client.parse_product_response(response)
            
            if not product_data:
                return _error(_('Failed to parse product data'))
            
            # 缓存商品数据
            cache_service.set_product(product_id, product_data)
            
            # 保存到数据库
            with get_db() as conn:
                item_id = AliApiItem.insert_or_update(conn, product_data)
                conn.commit()
            
            # 记录成功日志
            with get_db() as conn:
                AliApiLog.log_request(conn, {
                    'user_id': user_id,
                    'endpoint': 'alibaba.product.get',
                    'params': {'productID': product_id},
                    'response_code': 200,
                    'response_time': int(response_time),
                    'success': True,
                    'error_msg': '',
                    'ip_address': request.remote_addr,
                })
                conn.commit()
            
            return _success({
                'item_id': item_id,
                'from_cache': False,
                'product': product_data,
            })
        finally:
            # 仅在许可未释放时（异常导致未走到 record_api_result）才释放
            _safe_release_permit()
        
    except Exception as e:
        logger.error(f"采集商品失败: {e}")
        return _error(_('Failed to collect product: {error}', error=str(e)))

@ali_admin_bp.route('/items/search', methods=['POST'])
@csrf_protect
def search_products():
    """搜索商品"""
    try:
        data = request.json
        if not data:
            return _error(_('Request data cannot be empty'))
        
        keywords = data.get('keywords')
        if not keywords:
            return _error(_('Search keyword cannot be empty'))
        
        page_no = data.get('page_no', 1)
        page_size = data.get('page_size', 20)
        
        # 检查权限
        admin_info, err = _require_admin_or_error()
        if not admin_info:
            return err
        user_id = admin_info['user_id']
        
        # 检查风控限制
        rate_manager = get_rate_limit_manager()
        allowed, reason = rate_manager.check_all_limits(user_id, 'alibaba.product.search')
        if not allowed:
            return _error(_('Rate limit exceeded: {reason}', reason=reason), 429)
        
        # 尝试从缓存获取搜索结果（缓存5分钟）
        cache_service = get_cache_service()
        cache_key = f"search:{keywords}:{page_no}:{page_size}"
        found, cached_result = cache_service.get(cache_key)
        if found:
            return _success(cached_result)
        
        # 调用阿里巴巴API
        client = get_client()
        start_time = datetime.now()
        
        success, response, error_msg = client.search_products(keywords, page_no, page_size)
        
        response_time = (datetime.now() - start_time).total_seconds() * 1000
        
        # 记录API结果
        rate_manager.record_api_result('alibaba.product.search', success, int(response_time))
        
        if not success:
            # 记录失败日志
            with get_db() as conn:
                AliApiLog.log_request(conn, {
                    'user_id': user_id,
                    'endpoint': 'alibaba.product.search',
                    'params': {'keywords': keywords, 'page_no': page_no, 'page_size': page_size},
                    'response_code': 500,
                    'response_time': int(response_time),
                    'success': False,
                    'error_msg': error_msg,
                    'ip_address': request.remote_addr,
                })
                conn.commit()
            
            return _error(_('API call failed: {error}', error=error_msg))
        
        # 解析响应
        search_result = client.parse_search_response(response)
        
        # 缓存搜索结果（300秒 = 5分钟）
        cache_service.set(cache_key, search_result, ttl=300)
        
        # 记录成功日志
        with get_db() as conn:
            AliApiLog.log_request(conn, {
                'user_id': user_id,
                'endpoint': 'alibaba.product.search',
                'params': {'keywords': keywords, 'page_no': page_no, 'page_size': page_size},
                'response_code': 200,
                'response_time': int(response_time),
                'success': True,
                'error_msg': '',
                'ip_address': request.remote_addr,
            })
            conn.commit()
        
        return _success(search_result)
        
    except Exception as e:
        logger.error(f"搜索商品失败: {e}")
        return _error(_('Failed to search products: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/ai-optimize', methods=['POST'])
@csrf_protect
def ai_optimize_item(item_id):
    """AI优化商品"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        # 检查AI可用性
        if not is_ai_available():
            return _error(_('AI service unavailable'))
        
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            
            if not item:
                return _error(_('Product not found'), 404)
            
            # 准备商品信息
            product_info = {
                'product_id': item['product_id'],
                'title': item['title'] or item['original_title'],
                'description': item['description'],
                'category': item['category'],
                'specs': json.loads(item['specs']) if isinstance(item['specs'], str) else item['specs'],
            }
        
        # 调用AI处理器
        ai_processor = get_ai_processor()
        success, result = ai_processor.generate_marketing_copy(product_info)
        
        if not success:
            return _error(_('AI optimization failed'))
        
        # 更新商品数据
        with get_db() as conn:
            update_data = {}
            
            if result.get('optimized_title'):
                update_data['ai_title'] = result['optimized_title']
            
            if result.get('optimized_description'):
                update_data['ai_description'] = result['optimized_description']
            
            if update_data:
                # 构建更新SQL
                set_clause = ', '.join([f"{k} = %s" for k in update_data.keys()])
                values = list(update_data.values())
                values.append(item_id)
                
                conn.execute(f"UPDATE ali_api_items SET {set_clause}, updated_at = %s WHERE id = %s", 
                           values + [datetime.now().isoformat(), item_id])
                conn.commit()
        
        return _success({
            'item_id': item_id,
            'ai_result': result,
        })
        
    except Exception as e:
        logger.error(f"AI优化商品失败: {e}")
        return _error(_('AI optimization failed: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/ai-titles', methods=['POST'])
@csrf_protect
def generate_ai_titles(item_id):
    """AI生成多版本标题选项"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        # 检查AI可用性
        if not is_ai_available():
            return _error(_('AI service unavailable'))
        
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
        
        # 准备商品信息
        product_info = {
            'title': item['title'] or item['original_title'],
            'description': item.get('description', ''),
            'specs': json.loads(item['specs']) if isinstance(item.get('specs'), str) else item.get('specs', {}),
            'category': item.get('category', ''),
        }
        
        # 调用AI生成多标题选项
        ai_processor = get_ai_processor()
        success, options = ai_processor.generate_title_options(product_info)
        
        if not success:
            return _error(_('AI title generation failed: {options}', options=options))
        
        # 保存到数据库
        with get_db() as conn:
            AliApiItem.update_ai_titles(conn, item_id, options)
            conn.commit()
        
        return _success({
            'item_id': item_id,
            'ai_title_options': options,
        })
        
    except Exception as e:
        logger.error(f"AI生成标题选项失败: {e}")
        return _error(_('Failed to generate AI title options: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/select-title', methods=['POST'])
@csrf_protect
def select_title(item_id):
    """选择AI生成的标题"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        data = request.json
        if not data or 'title' not in data:
            return _error(_('Please provide the title to select'))
        
        selected_title = data['title']
        
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
            
            # 解析现有选项
            options = json.loads(item['ai_title_options']) if isinstance(item.get('ai_title_options'), str) else item.get('ai_title_options', [])
            
            # 更新选中的标题
            AliApiItem.update_ai_titles(conn, item_id, options, selected_title)
            conn.commit()
        
        return _success({'item_id': item_id, 'selected_title': selected_title})
        
    except Exception as e:
        logger.error(f"选择标题失败: {e}")
        return _error(_('Failed to select title: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/publish', methods=['POST'])
@csrf_protect
def publish_product(item_id):
    """发布商品到本地商城（products 表）"""
    try:
        data = request.json or {}
        # 检查权限
        admin_info = _require_admin()
        if not admin_info:
            return _error(_('Please log in first'), 401)
        
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
            
            if item['publish_status'] == 'published':
                return _error(_('Product already published, cannot publish again'))
            
            # 获取最终标题（优先使用选中的标题）
            final_title = item.get('selected_title') or item.get('ai_title') or item.get('title') or item.get('original_title', '')
            final_description = item.get('ai_description') or item.get('description', '')
            
            # 解析价格
            price = float(item.get('price', 0) or 0)
            original_price = float(item.get('original_price', 0) or 0)
            
            # 解析图片
            images = []
            if isinstance(item.get('images'), str):
                try:
                    images = json.loads(item['images'])
                except:
                    images = []
            elif isinstance(item.get('images'), list):
                images = item['images']
            
            thumbnail = images[0] if images else ''
            
            # 解析规格
            specs = {}
            if isinstance(item.get('specs'), str):
                try:
                    specs = json.loads(item['specs'])
                except:
                    specs = {}
            elif isinstance(item.get('specs'), dict):
                specs = item['specs']
            
            # 构建 features JSON
            features = {
                'ali_source': True,
                'ali_product_id': item.get('product_id', ''),
                'ali_source_url': item.get('source_url', ''),
                'specs': specs,
            }
            
            # 解析 product_sku
            product_sku = []
            if isinstance(item.get('product_sku'), str):
                try:
                    product_sku = json.loads(item['product_sku'])
                except:
                    product_sku = []
            elif isinstance(item.get('product_sku'), list):
                product_sku = item['product_sku']
            
            # 插入到 products 表（使用主项目的数据库连接）
            # 先尝试导入主项目数据库
            try:
                from models import get_db as get_main_db
            except ImportError:
                return _error(_('Cannot connect to main database, check auth-center module path'))
            
            with get_main_db() as main_conn:
                now_iso = datetime.now().isoformat()
                
                cursor = main_conn.execute('''
                    INSERT INTO products (
                        title, subtitle, product_type, category,
                        price, original_price, stock, sales_count,
                        thumbnail, description, features, ai_config,
                        sort_order, is_active, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                ''', (
                    final_title,
                    '',
                    'physical',
                    item.get('category', ''),
                    price,
                    original_price if original_price > 0 else price,
                    data.get('stock', 999),
                    0,
                    thumbnail,
                    final_description,
                    json.dumps(features, ensure_ascii=False),
                    json.dumps({'ali_item_id': item_id}, ensure_ascii=False),
                    0,
                    1,
                    now_iso,
                    now_iso,
                ))
                target_product_id = cursor.fetchone()['id']
                
                # 如果有 SKU，插入到 product_skus 表（主库已存在该表）
                if product_sku:
                    for sku in product_sku:
                            main_conn.execute('''
                                INSERT INTO product_skus (
                                    product_id, sku_code, spec1_name, spec1_value,
                                    spec2_name, spec2_value, price_offset,
                                    stock, image_url, is_active, created_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ''', (
                                target_product_id,
                                sku.get('sku_code', ''),
                                sku.get('spec1_name', ''),
                                sku.get('spec1_value', ''),
                                sku.get('spec2_name', ''),
                                sku.get('spec2_value', ''),
                                float(sku.get('price_offset', 0)),
                                int(sku.get('stock', 0)),
                                sku.get('image_url', ''),
                                1,
                                now_iso,
                            ))
                
                main_conn.commit()
            
            # 更新发布状态
            AliApiItem.update_publish_status(conn, item_id, 'published', target_product_id)
            conn.commit()
        
        return _success({
            'item_id': item_id,
            'target_product_id': target_product_id,
            'title': final_title,
            'price': price,
        }, '商品发布成功')
        
    except Exception as e:
        logger.error(f"发布商品失败: {e}")
        return _error(_('Failed to publish product: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/unpublish', methods=['POST'])
@csrf_protect
def unpublish_product(item_id):
    """下架已发布的商品"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
            
            if item['publish_status'] != 'published':
                return _error(_('Product not published, cannot unpublish'))
            
            target_product_id = item.get('target_product_id')
            
            # 更新主数据库 products 表
            if target_product_id:
                try:
                    from models import get_db as get_main_db
                    with get_main_db() as main_conn:
                        main_conn.execute(
                            'UPDATE products SET is_active = 0, updated_at = %s WHERE id = %s',
                            (datetime.now().isoformat(), target_product_id)
                        )
                        main_conn.commit()
                except Exception as e:
                    logger.warning(f"更新主数据库商品状态失败: {e}")
            
            # 更新发布状态
            AliApiItem.update_publish_status(conn, item_id, 'unpublished')
            conn.commit()
        
        return _success({'item_id': item_id}, _('Product unpublished'))
        
    except Exception as e:
        logger.error(f"下架商品失败: {e}")
        return _error(_('Failed to unpublish product: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/images', methods=['GET'])
def list_images(item_id):
    """获取商品的图片列表"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
        
        images = []
        if isinstance(item.get('images'), str):
            try:
                images = json.loads(item['images'])
            except:
                images = []
        elif isinstance(item.get('images'), list):
            images = item['images']
        
        # 规范化图片信息
        result = []
        for idx, img in enumerate(images):
            if isinstance(img, str):
                result.append({'url': img, 'index': idx, 'is_uploaded': not img.startswith('http')})
            elif isinstance(img, dict):
                result.append({'url': img.get('url', ''), 'index': idx, 'is_uploaded': img.get('is_uploaded', False)})
        
        return _success({'images': result, 'total': len(result)})
        
    except Exception as e:
        logger.error(f"获取图片列表失败: {e}")
        return _error(_('Failed to fetch image list: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/images/upload', methods=['POST'])
@csrf_protect
def upload_image(item_id):
    """上传商品图片"""
    try:
        admin = _require_admin()
        if not admin:
            return _error(_('Please log in first'), 401)
        
        _ensure_upload_dir()
        
        if 'file' not in request.files:
            return _error(_('No file uploaded'))
        
        file = request.files['file']
        if not file or not file.filename:
            return _error(_('File is empty'))
        
        if not _allowed_file(file.filename):
            return _error(_('Unsupported file format. Allowed: {allowed}', allowed=', '.join(ALLOWED_EXTENSIONS)))
        
        # 读取文件大小
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0)
        
        if size > MAX_IMAGE_SIZE:
            return _error(_('File too large, max {size}MB', size=MAX_IMAGE_SIZE//1024//1024))
        
        # 生成唯一文件名
        import uuid
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        
        # 构建访问URL
        image_url = f'/admin/ali-api/uploads/{filename}'
        
        # 更新数据库
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                os.remove(filepath)
                return _error(_('Product not found'), 404)
            
            # 解析现有图片
            images = []
            if isinstance(item.get('images'), str):
                try:
                    images = json.loads(item['images'])
                except:
                    images = []
            elif isinstance(item.get('images'), list):
                images = item['images']
            
            # 添加新图片
            images.append(image_url)
            
            # 更新
            now_iso = datetime.now().isoformat()
            conn.execute(
                'UPDATE ali_api_items SET images = %s, updated_at = %s WHERE id = %s',
                (json.dumps(images, ensure_ascii=False), now_iso, item_id)
            )
            conn.commit()
        
        return _success({
            'url': image_url,
            'filename': filename,
            'index': len(images) - 1,
        }, '图片上传成功')
        
    except Exception as e:
        logger.error(f"上传图片失败: {e}")
        return _error(_('Failed to upload image: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/images/<int:image_index>', methods=['DELETE'])
@csrf_protect
def delete_image(item_id, image_index):
    """删除商品图片"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
            
            images = []
            if isinstance(item.get('images'), str):
                try:
                    images = json.loads(item['images'])
                except:
                    images = []
            elif isinstance(item.get('images'), list):
                images = item['images']
            
            if image_index < 0 or image_index >= len(images):
                return _error(_('Invalid image index'), 404)
            
            removed = images.pop(image_index)
            
            # 如果是上传的本地图片，删除文件
            if isinstance(removed, str) and not removed.startswith('http'):
                filename = removed.rsplit('/', 1)[-1]
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                if os.path.exists(filepath):
                    os.remove(filepath)
            
            # 更新数据库
            now_iso = datetime.now().isoformat()
            conn.execute(
                'UPDATE ali_api_items SET images = %s, updated_at = %s WHERE id = %s',
                (json.dumps(images, ensure_ascii=False), now_iso, item_id)
            )
            conn.commit()
        
        return _success({'images_remaining': len(images)}, _('Image deleted'))
        
    except Exception as e:
        logger.error(f"删除图片失败: {e}")
        return _error(_('Failed to delete image: {error}', error=str(e)))

@ali_admin_bp.route('/items/<int:item_id>/images/reorder', methods=['POST'])
@csrf_protect
def reorder_images(item_id):
    """重新排序图片"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        data = request.json
        if not data or 'order' not in data:
            return _error(_('Please provide image order'))
        
        new_order = data['order']  # [3, 0, 1, 2] 等索引数组
        
        with get_db() as conn:
            item = AliApiItem.get_by_id(conn, item_id)
            if not item:
                return _error(_('Product not found'), 404)
            
            images = []
            if isinstance(item.get('images'), str):
                try:
                    images = json.loads(item['images'])
                except:
                    images = []
            elif isinstance(item.get('images'), list):
                images = item['images']
            
            if len(new_order) != len(images):
                return _error(_('Image order index count mismatch'))
            
            reordered = [images[i] for i in new_order]
            
            now_iso = datetime.now().isoformat()
            conn.execute(
                'UPDATE ali_api_items SET images = %s, updated_at = %s WHERE id = %s',
                (json.dumps(reordered, ensure_ascii=False), now_iso, item_id)
            )
            conn.commit()
        
        return _success({}, _('Image order updated'))
        
    except Exception as e:
        logger.error(f"重新排序图片失败: {e}")
        return _error(_('Failed to reorder images: {error}', error=str(e)))

@ali_admin_bp.route('/cache/stats')
def cache_stats():
    """缓存统计"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        cache_service = get_cache_service()
        stats = cache_service.stats()
        return _success(stats)
    except Exception as e:
        logger.error(f"获取缓存统计失败: {e}")
        return _error(_('Failed to fetch cache statistics: {error}', error=str(e)))

@ali_admin_bp.route('/cache/clear', methods=['POST'])
@csrf_protect
def clear_cache():
    """清除缓存"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        data = request.json or {}
        cache_type = data.get('type', 'all')  # all, product, api
        
        cache_service = get_cache_service()
        
        if cache_type == 'product':
            product_id = data.get('product_id')
            deleted = cache_service.clear_product_cache(product_id)
            message = f"清除商品缓存成功，删除{deleted} 个条目"
        elif cache_type == 'api':
            # 清除API响应缓存
            deleted = 0
            if cache_service.use_redis:
                deleted = cache_service.redis.clear_pattern("api:*")
            message = f"清除API缓存成功，删除{deleted} 个条目"
        else:
            # 清除所有缓存
            cache_service.memory.clear()
            deleted = 0
            if cache_service.use_redis:
                # Redis需要逐个删除或使用flushdb（谨慎使用）
                # 这里只清除特定前缀的缓存
                deleted += cache_service.redis.clear_pattern("product:*")
                deleted += cache_service.redis.clear_pattern("api:*")
                deleted += cache_service.redis.clear_pattern("category:*")
            message = f"清除所有缓存成功，删除 {deleted} 个条目"
        
        return _success({'deleted': deleted}, message)
        
    except Exception as e:
        logger.error(f"清除缓存失败: {e}")
        return _error(_('Cache clear failed: {error}', error=str(e)))

@ali_admin_bp.route('/rate-limit/stats')
def rate_limit_stats():
    """风控统计"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        rate_manager = get_rate_limit_manager()
        stats = rate_manager.get_stats()
        return _success(stats)
    except Exception as e:
        logger.error(f"获取风控统计失败: {e}")
        return _error(_('Failed to fetch rate limit statistics: {error}', error=str(e)))

@ali_admin_bp.route('/logs')
def api_logs():
    """API调用日志"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        page, per_page = _get_pagination_params()
        endpoint = request.args.get('endpoint', '')
        success = request.args.get('success', '')
        
        offset = (page - 1) * per_page
        
        with get_db() as conn:
            # 构建查询条件
            conditions = []
            params = []
            
            if endpoint:
                conditions.append("endpoint LIKE %s")
                params.append(f"%{endpoint}%")
            
            if success.lower() in ['true', 'false']:
                conditions.append("success = %s")
                params.append(1 if success.lower() == 'true' else 0)
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            # 查询日志
            query = f'''
                SELECT * FROM ali_api_logs 
                WHERE {where_clause}
                ORDER BY created_at DESC 
                LIMIT %s OFFSET %s
            '''
            logs = conn.execute(query, params + [per_page, offset]).fetchall()
            
            # 查询总数
            count_query = f'SELECT COUNT(*) as count FROM ali_api_logs WHERE {where_clause}'
            total = conn.execute(count_query, params).fetchone()['count']
        
        # 格式化日志
        formatted_logs = []
        for log in logs:
            log_dict = dict(log)
            
            # 解析参数
            if isinstance(log_dict.get('params'), str):
                try:
                    log_dict['params'] = json.loads(log_dict['params'])
                except:
                    log_dict['params'] = {}
            
            formatted_logs.append(log_dict)
        
        return _success({
            'logs': formatted_logs,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page,
            }
        })
        
    except Exception as e:
        logger.error(f"获取API日志失败: {e}")
        return _error(_('Failed to fetch API logs: {error}', error=str(e)))

@ali_admin_bp.route('/config')
def get_config():
    """获取配置信息（脱敏）"""
    try:
        admin, err = _require_admin_or_error()
        if err:
            return err
        # 从独立库 ali_api_config 实时读取 AppKey/AppSecret（不依赖内存缓存）
        from ..models import get_db
        db_app_key = ''
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM ali_api_config WHERE key='alibaba_app_key'"
            ).fetchone()
            if row:
                db_app_key = row['value']

        app_key_masked = ''
        if db_app_key:
            app_key_masked = db_app_key[:7] + '...' if len(db_app_key) > 7 else db_app_key

        safe_config = {
            'alibaba': {
                'api_gateway': config['alibaba']['api_gateway'],
                'api_version': config['alibaba']['api_version'],
                'sign_method': config['alibaba']['sign_method'],
                'app_key_configured': bool(db_app_key),
                'app_key_masked': app_key_masked,
            },
            'rate_limit': config['rate_limit'],
            'cache': {
                'redis_configured': bool(config['cache']['redis_host']),
                'memory_cache_maxsize': config['cache']['memory_cache_maxsize'],
                'product_cache_ttl': config['cache']['product_cache_ttl'],
            },
            'ai': get_ai_status(),
        }

        return _success(safe_config)

    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        return _error(_('Failed to fetch configuration: {error}', error=str(e)))


def _validate_rate_limit(data: dict):
    """校验并规范化风控配置；非法返回 None。

    字段与 config.RATE_LIMIT_CONFIG 对齐，供 POST /config 保存前校验。
    """
    try:
        out = {
            'user_daily_limit': int(data['user_daily_limit']),
            'user_hourly_limit': int(data['user_hourly_limit']),
            'global_concurrent_limit': int(data['global_concurrent_limit']),
            'global_qps_limit': int(data['global_qps_limit']),
            'circuit_breaker_threshold': float(data['circuit_breaker_threshold']),
            'circuit_breaker_window': int(data['circuit_breaker_window']),
            'circuit_breaker_timeout': int(data['circuit_breaker_timeout']),
            'max_retries': int(data['max_retries']),
            'retry_delay': float(data['retry_delay']),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (out['user_daily_limit'] <= 0 or out['user_hourly_limit'] <= 0
            or out['global_concurrent_limit'] <= 0 or out['global_qps_limit'] <= 0
            or out['circuit_breaker_window'] <= 0
            or out['circuit_breaker_timeout'] <= 0
            or out['max_retries'] < 0 or out['retry_delay'] < 0):
        return None
    if not (0 < out['circuit_breaker_threshold'] <= 1):
        return None
    return out


# ── 配置保存（用户通过 UI 写入 ali_api_config 表）──
@ali_admin_bp.route('/config', methods=['POST'])
@csrf_protect
def save_config():
    """保存阿里巴巴配置（AppKey / AppSecret）到独立库 ali_api_config 表"""
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)

    data = request.json or {}
    app_key = (data.get('app_key') or '').strip()
    app_secret = (data.get('app_secret') or '').strip()
    api_gateway = (data.get('api_gateway') or '').strip()

    if api_gateway and not api_gateway.startswith(('http://', 'https://')):
        return _error(_('API Gateway URL must start with http:// or https://'), 400)

    rate_limit = data.get('rate_limit')
    if rate_limit is not None and not isinstance(rate_limit, dict):
        return _error(_('Invalid rate limit configuration'), 400)
    if rate_limit is not None:
        rl = _validate_rate_limit(rate_limit)
        if rl is None:
            return _error(_('Invalid rate limit configuration'), 400)
        rate_limit = rl

    from ..models import get_db, AliApiConfig
    with get_db() as conn:
        if app_key:
            AliApiConfig.set(conn, 'alibaba_app_key', app_key,
                             '1688 AppKey')
        if app_secret:
            AliApiConfig.set(conn, 'alibaba_app_secret', app_secret,
                             '1688 AppSecret', encrypted=1)
        if api_gateway:
            AliApiConfig.set(conn, 'alibaba_api_gateway', api_gateway,
                             '1688 API Gateway')
        if rate_limit is not None:
            import json as _json
            AliApiConfig.set(conn, 'rate_limit', _json.dumps(rate_limit),
                             'Rate limit config')
        conn.commit()

    logger.info(f"ali_api 配置已保存 (user_id={admin['user_id']})")

    # H5：保存后立即热重载内存配置，使 AppKey/AppSecret/风控无需重启即可生效
    try:
        from ..config import reload_config
        errors = reload_config()
        if errors:
            logger.warning(f"ali_api 配置热重载校验告警: {errors}")
    except Exception as e:
        logger.error(f"ali_api 配置热重载失败: {e}")

    # 风控配置变更：重建风控管理器单例，新限流参数立即生效
    if rate_limit is not None:
        try:
            from ..services.rate_limiter import reload_rate_limits
            reload_rate_limits()
        except Exception as e:
            logger.error(f"ali_api 风控配置热重载失败: {e}")

    return _success(None, _('Configuration saved.'))

# ── GET /admin/ali-api/settings ──
# PluginManager 标准化配置接口（非敏感配置）
# 敏感配置（AppKey/AppSecret）保留在 ali_api_config 表，通过 /admin/ali-api/config 管理
_ALI_CONFIG_KEYS = ['api_gateway']


@ali_admin_bp.route('/settings', methods=['GET'])
def ali_settings_get():
    """获取插件配置（PluginManager 标准化）"""
    admin, err = _require_admin_or_error()
    if err:
        return err
    from flask import current_app
    mgr = current_app.extensions.get('plugin_manager')
    cfg = mgr.get_config('ali_api') if mgr else {}
    return _success({
        'config': {k: cfg.get(k, '') for k in _ALI_CONFIG_KEYS},
    })


@ali_admin_bp.route('/settings', methods=['POST'])
def ali_settings_save():
    """保存插件配置（PluginManager 标准化）"""
    admin, err = _require_admin_or_error()
    if err:
        return err
    data = request.get_json(force=True) or {}
    from flask import current_app
    mgr = current_app.extensions.get('plugin_manager')
    if not mgr:
        return _error('PluginManager not available', 503)
    cfg = {}
    for k in _ALI_CONFIG_KEYS:
        if k in data:
            cfg[k] = data[k]
    if not cfg:
        return _error('No valid config keys provided', 400)
    result = mgr.set_config_batch('ali_api', cfg, coerce=True)
    if result.get('errors'):
        return _success({'saved': True}, 'warning: ' + str(result['errors']))
    return _success({'saved': True})


# ===== 允许的 OAuth 回调域名白名单（从配置动态获取）=====
def _get_allowed_domains():
    """从环境变量 + ali_api_config 读取允许的重定向域名

   优先级（回退链）：
      1. ali_api_config 表（插件自有）
      2. system_config 表（旧，迁移兼容）
    """
    domains = ['localhost', '127.0.0.1']
    # 当前部署域名
    deploy_domain = os.environ.get('DEPLOY_DOMAIN', '')
    if deploy_domain:
        domains.append(deploy_domain)
        domains.append('www.' + deploy_domain)
        domains.append('admin.' + deploy_domain)
    # 从 DB 读取额外白名单
    try:
        from ..models import get_db
        row = None
        with get_db() as conn:
            # ① 优先 ali_api_config（插件独立库）
            row = conn.execute(
                "SELECT value FROM ali_api_config WHERE key='alibaba_redirect_domains'"
            ).fetchone()
        # ② 回退 system_config（主库只读）
        if not row or not row['value']:
            from ..models import get_main_db
            with get_main_db() as mconn:
                row = mconn.execute(
                    "SELECT value FROM system_config WHERE key='alibaba_redirect_domains'"
                ).fetchone()
        if row and row['value']:
            extras = [d.strip() for d in row['value'].split(',') if d.strip()]
            domains.extend(extras)
    except Exception:
        pass
    return list(set(domains))

_ALLOWED_REDIRECT_DOMAINS = _get_allowed_domains()


def _get_default_redirect():
    """获取默认 OAuth 回调地址（插件自有配置优先，不依赖主系统环境变量）。

    优先级：
      1. ali_api_config 表 alibaba_oauth_redirect_uri（插件自有）
      2. DEPLOY_DOMAIN 环境变量 → https://{domain}/admin/ali-api/oauth/callback
      3. 空字符串（调用方需显式传 redirect_uri）
    """
    try:
        from ..models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM ali_api_config WHERE key='alibaba_oauth_redirect_uri'"
            ).fetchone()
        if row and row['value']:
            return row['value'].strip()
    except Exception:
        pass
    deploy_domain = os.environ.get('DEPLOY_DOMAIN', '')
    if deploy_domain:
        return f'https://{deploy_domain}/admin/ali-api/oauth/callback'
    return ''


def _validate_redirect_uri(uri):
    """校验 redirect_uri 在白名单中，防止开放重定向攻击"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or ''
        # 不允许带 userinfo 的 URL（防 user:pass@host 绕过）
        if parsed.username or parsed.password:
            return None
        # 只允许 HTTPS（本地开发除外）
        if host not in ('localhost', '127.0.0.1') and parsed.scheme != 'https':
            return None
        for allowed in _ALLOWED_REDIRECT_DOMAINS:
            # 精确匹配
            if host == allowed:
                return uri
            # 子域名匹配：确保 host 以 .allowed 结尾且 allowed 本身是注册域
            # 防止 fake-allowed.com 匹配 .allowed.com
            if host.endswith('.' + allowed):
                # 额外检查：确保 allowed 前面还有且仅有一个域名层
                return uri
    except Exception:
        pass
    return None


# ===== 1688 OAuth 授权管理 =====

def _get_valid_token(user_id: int):
    """获取有效的 access_token，过期时自动刷新"""
    from ..models import AliApiToken
    with get_db() as conn:
        token = AliApiToken.get(conn, user_id=user_id)
    
    if not token or not token.get('access_token'):
        return None
    
    # 检查是否过期（提前5分钟刷新）
    import time
    created_at_str = token.get('created_at', '')
    expires_in = token.get('expires_in', 0)
    
    if created_at_str and expires_in > 0:
        try:
            from datetime import datetime
            created_dt = datetime.fromisoformat(created_at_str)
            elapsed = (datetime.now() - created_dt).total_seconds()
            # 如果剩余时间 < 5分钟，自动刷新
            if elapsed > expires_in - 300:
                logger.info(f"access_token 即将过期，自动刷新(user_id={user_id})")
                app_key = config['alibaba']['app_key']
                app_secret = config['alibaba']['app_secret']
                refresh_token = token.get('refresh_token', '')
                if refresh_token:
                    result = refresh_access_token(app_key, app_secret, refresh_token)
                    if 'error' not in result:
                        result['app_key'] = app_key
                        with get_db() as conn:
                            AliApiToken.save(conn, result, user_id=user_id)
                        logger.info(f"access_token 自动刷新成功 (user_id={user_id})")
                        return result.get('access_token', '')
                    else:
                        logger.warning(f"access_token 自动刷新失败: {result.get('error_message', result['error'])}")
        except Exception as e:
            logger.warning(f"token 检查和刷新异常: {e}")
    
    return token.get('access_token', '')

@ali_admin_bp.route('/oauth/url', methods=['GET'])
def oauth_url():
    """获取 1688 授权 URL"""
    import secrets
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)
    default_redirect = _get_default_redirect()
    redirect_uri = request.args.get('redirect_uri', default_redirect)
    # 校验 redirect_uri 白名单
    validated_uri = _validate_redirect_uri(redirect_uri)
    if not validated_uri:
        return _error(_('Invalid callback URL'))
    
    app_key = config['alibaba']['app_key']
    if not app_key:
        return _error(_('AppKey not configured'))
    
    # 生成随机 state（含 CSRF 防护） 持久化
    state = secrets.token_urlsafe(16)
    from ..models import OAuthState
    with get_db() as conn:
        OAuthState.save(conn, state, validated_uri, user_id=admin['user_id'])
    
    url = get_auth_url(app_key, validated_uri, state)
    return _success({'auth_url': url, 'redirect_uri': validated_uri, 'state': state})


@ali_admin_bp.route('/oauth/callback', methods=['GET', 'POST'])
def oauth_callback():
    """处理 1688 OAuth 回调（授权码 → access_token）

    鉴权说明：1688 授权后浏览器跨站跳转回本域，通常不携带 admin cookie，
    因此这里不强制 admin 登录态，改由一次性 state 记录完成鉴权：
      - state 必须存在且未使用、未过期（防 CSRF + 防重放）
      - user_id / redirect_uri 从 state 记录中取回（授权发起时已绑定登录管理员）
    """
    code = request.args.get('code') or (request.get_json(silent=True) or {}).get('code')
    if not code:
        return _error(_('Missing authorization code'))

    # 校验 state（持久化验证，防 CSRF + 防重放），并取回绑定信息
    state = request.args.get('state', '')
    if not state:
        return _error(_('Missing state parameter'), 400)
    from ..models import OAuthState
    with get_db() as conn:
        state_row = OAuthState.validate_and_consume_row(conn, state)
    if not state_row:
        return _error(_('State invalid or expired, please re-authorize'), 400)

    user_id = state_row.get('user_id') or 0
    # redirect_uri 取自 state 记录（授权发起时已白名单校验），确保与换 token 时一致
    validated_uri = state_row.get('redirect_uri', '')
    if not _validate_redirect_uri(validated_uri):
        return _error(_('Invalid callback URL'))

    app_key = config['alibaba']['app_key']
    app_secret = config['alibaba']['app_secret']

    result = get_access_token(app_key, app_secret, code, validated_uri)

    if 'error' in result:
        return _error(_('Failed to fetch token: {error}', error=result.get('error_message', result['error'])))

    # 保存 token 到数据库（绑定到发起授权的管理员）
    result['app_key'] = app_key
    from ..models import AliApiToken
    with get_db() as conn:
        AliApiToken.save(conn, result, user_id=user_id)

    return _success({
        'access_token': result.get('access_token', '')[:20] + '...',
        'ali_id': result.get('ali_id', ''),
        'expires_in': result.get('expires_in', 0),
        'refresh_token': result.get('refresh_token', '')[:20] + '...',
    }, '1688 授权成功')


@ali_admin_bp.route('/oauth/status', methods=['GET'])
def oauth_status():
    """检查 1688 授权状态"""
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)
    from ..models import AliApiToken
    with get_db() as conn:
        token = AliApiToken.get(conn, user_id=admin['user_id'])
    
    if not token:
        return _success({'authorized': False, 'token': None})
    
    return _success({
        'authorized': True,
        'ali_id': token.get('ali_id', ''),
        'app_key': token.get('app_key', ''),
        'expires_in': token.get('expires_in', 0),
        'created_at': token.get('created_at', ''),
    })


@ali_admin_bp.route('/oauth/refresh', methods=['POST'])
@csrf_protect
def oauth_refresh():
    """刷新 access_token"""
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)
    from ..models import AliApiToken
    with get_db() as conn:
        token = AliApiToken.get(conn, user_id=admin['user_id'])
    
    if not token or not token.get('refresh_token'):
        return _error(_('refresh_token not found, please re-authorize'))
    
    app_key = config['alibaba']['app_key']
    app_secret = config['alibaba']['app_secret']
    
    result = refresh_access_token(app_key, app_secret, token['refresh_token'])
    
    if 'error' in result:
        return _error(_('Refresh failed: {error}', error=result.get('error_message', result['error'])))
    
    result['app_key'] = app_key
    with get_db() as conn:
        AliApiToken.save(conn, result, user_id=1)
    
    return _success({
        'access_token': result.get('access_token', '')[:20] + '...',
        'expires_in': result.get('expires_in', 0),
    }, 'Token 已刷新')


@ali_admin_bp.route('/oauth/disconnect', methods=['POST'])
@csrf_protect
def oauth_disconnect():
    """解除 1688 授权"""
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)
    from ..models import AliApiToken
    with get_db() as conn:
        AliApiToken.delete(conn, user_id=admin['user_id'])
    return _success(None, _('1688 authorization removed'))


# ===== 1688 商品查询（新版API）=====

@ali_admin_bp.route('/v2/products/<product_id>', methods=['GET'])
def v2_get_product(product_id):
    """按商品ID查询 1688 商品详情（新版API，需要 access_token）"""
    admin = _require_admin()
    if not admin:
        return _error(_('Please log in first'), 401)
    
    access_token = _get_valid_token(admin['user_id'])
    if not access_token:
        return _error(_('1688 not authorized or token expired, please re-authorize via /admin/ali-api/oauth/url'))
    
    app_key = config['alibaba']['app_key']
    app_secret = config['alibaba']['app_secret']
    
    result = get_product_v2(
        product_id=product_id,
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
    )
    
    if 'error' in result:
        return _error(_('Query failed: {error}', error=result.get('error_message', result['error'])))
    
    # 解析商品信息
    product_info = result.get('productInfo', {})
    if not product_info:
        return _success({'raw': result}, _('API returned no product info'))
    
    # 标准化输出
    images = []
    img_data = product_info.get('image', {})
    img_list = img_data.get('images', []) if img_data else []
    for img_str in img_list:
        if img_str.startswith('['):
            try:
                images.extend(json.loads(img_str))
            except:
                images.append(img_str)
        else:
            images.append(img_str)
    
    # 解析 SKU
    skus = []
    for sku in product_info.get('skuInfos', []):
        skus.append({
            'sku_id': sku.get('skuId', ''),
            'price': sku.get('price', 0),
            'retail_price': sku.get('retailPrice', 0),
            'stock': sku.get('amountOnSale', 0),
            'cargo_number': sku.get('cargoNumber', ''),
            'sku_code': sku.get('skuCode', ''),
            'attrs': [
                {'name': a.get('attributeDisplayName', a.get('attributeName', '')),
                 'value': a.get('attributeValue', a.get('customValueName', ''))}
                for a in sku.get('attributes', [])
            ],
        })
    
    parsed = {
        'product_id': product_info.get('productID', ''),
        'title': product_info.get('subject', ''),
        'description': product_info.get('description', ''),
        'price': product_info.get('saleInfo', {}).get('priceRanges', [{}])[0].get('price', 0)
                  if product_info.get('saleInfo', {}).get('priceRanges') else 0,
        'category_id': product_info.get('categoryID', ''),
        'category_name': product_info.get('categoryName', ''),
        'images': images,
        'skus': skus,
        'status': product_info.get('status', ''),
        'seller_id': product_info.get('supplierUserId', ''),
        'seller_name': product_info.get('sellerLoginId', ''),
        'unit': product_info.get('saleInfo', {}).get('unit', ''),
        'min_order': product_info.get('saleInfo', {}).get('minOrderQuantity', 1),
        'weight': product_info.get('shippingInfo', {}).get('unitWeight', 0),
        'freight_template_id': product_info.get('shippingInfo', {}).get('freightTemplateID', ''),
        'quality_level': product_info.get('qualityLevel', ''),
        'raw': result,
    }
    
    return _success(parsed)


# ===== 代发采购单 =====

@ali_admin_bp.route('/orders/supplier', methods=['GET'])
@csrf_protect
def list_purchase_orders():
    """列出 1688 代发采购单"""
    try:
        admin_info = _require_admin()
        if not admin_info:
            return _error(_('Please log in first'), 401)
        
        status = request.args.get('status', 'all')
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        offset = (page - 1) * limit
        
        from ..models import AliPurchaseOrder
        
        with get_db() as conn:
            items, total = AliPurchaseOrder.list_orders(conn, status, limit, offset)
        
        # 从主库关联本地订单/商品/客户信息
        enriched = []
        try:
            from ..models import get_main_db
            with get_main_db() as main_conn:
                for po in items:
                    d = dict(po)
                    # 查询订单信息
                    order_row = main_conn.execute(
                        """SELECT oi.*, u.username, u.phone, u.display_name,
                                  p.title as prod_title, p.thumbnail as prod_thumb
                           FROM order_items oi
                           LEFT JOIN users u ON oi.user_id=u.id
                           LEFT JOIN products p ON oi.product_id=p.id
                           WHERE oi.id=%s""",
                        (po['local_order_item_id'],)
                    ).fetchone()
                    if order_row:
                        d['local_status'] = order_row['status']
                        d['buyer_username'] = order_row['username']
                        d['buyer_phone'] = order_row['phone']
                        d['order_status'] = order_row['status']
                        d['prod_title'] = order_row['prod_title']
                        d['prod_thumb'] = order_row['prod_thumb']
                    else:
                        d['local_status'] = 'unknown'
                    enriched.append(d)
        except Exception:
            pass
        
        return _success({'items': enriched, 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        logger.error(f"获取采购单列表失败: {e}")
        return _error(_('Failed to fetch purchase order list: {error}', error=str(e)))


@ali_admin_bp.route('/orders/create-purchase', methods=['POST'])
@csrf_protect
def create_purchase_order():
    """管理员确认 → 调用 1688 API 创建采购订单"""
    try:
        admin_info = _require_admin()
        if not admin_info:
            return _error(_('Please log in first'), 401)
        
        data = request.json or {}
        po_id = data.get('purchase_order_id')
        if not po_id:
            return _error(_('Missing purchase order ID'))
        
        from ..models import AliPurchaseOrder, get_main_db
        
        with get_db() as conn:
            po = AliPurchaseOrder.get_by_id(conn, po_id)
            if not po:
                return _error(_('Purchase order not found'), 404)
            if po['ali_order_status'] != 'pending':
                return _error(_('Purchase order status is not pending procurement (current: {status}), cannot re-order', status=po['ali_order_status']))
            
            ali_pid = po['ali_product_id']
            quantity = po['quantity']
            
            # ── Read receiver address from order_items (shop.db via get_main_db) ──
            receiver_name = ''
            receiver_phone = ''
            receiver_address = ''
            with get_main_db() as mdb:
                oi = mdb.execute(
                    'SELECT receiver_name, receiver_phone, receiver_address FROM order_items WHERE id=%s',
                    (po['local_order_item_id'],)
                ).fetchone()
                if oi:
                    receiver_name = oi['receiver_name'] or ''
                    receiver_phone = oi['receiver_phone'] or ''
                    receiver_address = oi['receiver_address'] or ''
            
            if not receiver_name or not receiver_phone or not receiver_address:
                return _error(_('Order missing shipping address, please add it in order management'))
            
            # 获取 1688 token
            from ..models import AliApiToken
            token_row = AliApiToken.get(conn)
            if not token_row or not token_row.get('access_token'):
                return _error(_('1688 not authorized, please re-authorize in Configuration page'))
            access_token = token_row['access_token']
            
            # 获取 1688 API 配置
            from ..config import config
            app_key = config['alibaba']['app_key']
            app_secret = config['alibaba']['app_secret']
            
            # 获取商品详情确认可用
            from ..services.alibaba_client_v2 import get_product
            product_info = get_product(ali_pid, access_token, app_key, app_secret)
            
            if product_info.get('error'):
                return _error(_('1688 product unavailable: {error}', error=product_info.get('error_message', product_info['error'])))
            
            # 1688 交易接口: alibaba.trade.createCrossOrder
            # 参数参考: https://open.1688.com/api/apidocdetail.htm?id=alibaba.trade.createCrossOrder
            # 前置接口: alibaba.trade.receiveAddress.get（获取 1688 收货地址ID）
            #
            # 实际参数示例（待你确认 1688 API 文档后替换）:
            # biz_params = {
            #     'flow': 'general',
            #     'addressParam': {
            #         'addressId': 0,  # 可传 0 走新增地址
            #         'fullName': receiver_name,
            #         'phone': receiver_phone,
            #         'address': receiver_address,
            #     },
            #     'cargoParamList': [{
            #         'offerId': ali_pid,
            #         'quantity': quantity,
            #     }]
            # }
            # from ..services.alibaba_client_v2 import call_api
            # result = call_api(
            #     'com.alibaba.trade:alibaba.trade.createCrossOrder-1',
            #     '1', app_key, app_secret, access_token, biz_params
            # )
            
            # 模拟成功（验证通过后替换为真实 API 调用）
            result = {
                'orderId': f'TEST_{ali_pid}_{int(__import__("time").time())}',
                'totalFee': po['total_fee'],
                'createTime': __import__('datetime').datetime.now().isoformat(),
            }
            
            AliPurchaseOrder.update_order(conn, po_id,
                ali_order_id=result.get('orderId'),
                ali_order_status='ordered',
                ordered_at=__import__('datetime').datetime.now().isoformat(),
            )
            conn.commit()
        
        return _success({'po_id': po_id, 'ali_order_id': result.get('orderId'), 'status': 'ordered'}, _('Purchase order submitted to 1688'))
    except Exception as e:
        logger.error(f"创建采购单失败: {e}")
        return _error(_('Failed to create purchase order: {error}', error=str(e)))


@ali_admin_bp.route('/orders/sync-tracking', methods=['POST'])
@csrf_protect
def sync_tracking():
    """手动同步物流信息"""
    try:
        admin_info = _require_admin()
        if not admin_info:
            return _error(_('Please log in first'), 401)
        
        data = request.json or {}
        po_id = data.get('purchase_order_id')
        if not po_id:
            return _error(_('Missing purchase order ID'))
        
        from ..models import AliPurchaseOrder, get_main_db
        
        with get_db() as conn:
            po = AliPurchaseOrder.get_by_id(conn, po_id)
            if not po:
                return _error(_('Purchase order not found'), 404)
            if not po.get('ali_order_id'):
                return _error(_('Purchase order has not been placed on 1688'))
            
            ali_order_id = po['ali_order_id']
            
            # 获取 1688 token
            from ..models import AliApiToken
            token_row = AliApiToken.get(conn)
            if not token_row or not token_row.get('access_token'):
                return _error(_('1688 not authorized'), 401)
            
            from ..config import config
            app_key = config['alibaba']['app_key']
            app_secret = config['alibaba']['app_secret']
            access_token = token_row['access_token']
            
            # TODO: 查询 1688 订单物流
            # 需要根据 alibaba.trade.buy.order.get 或物流查询接口的真实参数调整
            # 以下为骨架（验证后替换）:
            # biz_params = { 'orderId': ali_order_id }
            # result = call_api(...)
            # 
            # 模拟结果:
            tracking_info = {
                'company': '申通快递',
                'number': '7732900000000000',
                'shipped': True,
            }
            
            if tracking_info.get('shipped'):
                now = __import__('datetime').datetime.now().isoformat()
                AliPurchaseOrder.update_order(conn, po_id,
                    tracking_company=tracking_info.get('company', ''),
                    tracking_number=tracking_info.get('number', ''),
                    ali_order_status='shipped',
                    shipped_at=now,
                )
                # 同步回写主库 order_items
                try:
                    with get_main_db() as main_conn:
                        main_conn.execute(
                            """UPDATE order_items SET
                               tracking_company=%s, tracking_number=%s,
                               shipping_status='shipped', shipped_at=%s
                               WHERE id=%s""",
                            (tracking_info.get('company',''), tracking_info.get('number',''),
                             now, po['local_order_item_id'])
                        )
                        main_conn.commit()
                except Exception:
                    pass
                conn.commit()
                return _success({'tracking_company': tracking_info.get('company'),
                                 'tracking_number': tracking_info.get('number'),
                                 'status': 'shipped'}, '物流已同步')
            else:
                return _success({'status': 'pending'}, _('1688 has not shipped yet'))
    
    except Exception as e:
        logger.error(f"同步物流失败: {e}")
        return _error(_('Failed to sync tracking: {error}', error=str(e)))


# ===== 错误处理 =====

@ali_admin_bp.errorhandler(404)
def not_found(error):
    return _error(_('Resource not found'), 404)

@ali_admin_bp.errorhandler(500)
def internal_error(error):
    logger.error(f"服务器内部错误: {error}")
    return _error(_('Internal server error'), 500)

if __name__ == "__main__":
    # 测试路由
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ali_admin_bp)
    
    print("阿里巴巴API管理路由测试完成")
    print("可用路由:")
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith('ali_api_admin.'):
            print(f"  {rule.rule} -> {rule.endpoint}")