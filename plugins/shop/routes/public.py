#!/usr/bin/env python3
"""Shop Public — 前端商城API (platform service)"""
from i18n import _
import sys, os, json, logging

logger = logging.getLogger(__name__)
from flask import Blueprint, jsonify, request, render_template, make_response, redirect, current_app, session
from models import get_db
from services.jwt_service import validate_token
from plugin_manager.event_bus import get_event_bus, EventName
import secrets
from datetime import datetime

shop_public_bp = Blueprint('shop_public', __name__, url_prefix='/shop', static_folder='../static')


def _get_plugin_instance(name):
    """获取已启用插件实例，未启用返回 None"""
    pm = current_app.extensions.get('plugin_manager')
    if pm and pm.is_enabled(name):
        return pm.get_instance(name)
    return None


# 单件商品最大购买数量（防止超大数值导致溢出/超卖）
_MAX_ORDER_QTY = 999


# ── 限流 ──
# P1-2：限流已迁移到 PostgreSQL 滑动窗口（shop.rate_limits），
#    跨 gunicorn 多 worker 生效；DB 异常时放行，避免阻断正常业务。
import time as _time
_RL_CHECK_COUNT = 0  # 全局清理节流计数器


def _check_rate_limit(user_id, endpoint, max_requests=60, window=60):
    """每 user+endpoint 在 window 秒内最多 max_requests 次（DB 计数，跨进程生效）"""
    global _RL_CHECK_COUNT
    key = f'{user_id}:{endpoint}'
    now = _time.time()
    cutoff = now - window
    try:
        with get_db() as conn:
            # 惰性清理：删除当前 key 窗口外记录，控制单 key 数据量
            conn.execute('DELETE FROM shop.rate_limits WHERE rkey=%s AND ts<%s', (key, cutoff))
            cnt = conn.execute(
                'SELECT COUNT(*) AS c FROM shop.rate_limits WHERE rkey=%s AND ts>=%s',
                (key, cutoff)
            ).fetchone()['c']
            if cnt >= max_requests:
                return False
            conn.execute('INSERT INTO shop.rate_limits (rkey, ts) VALUES (%s,%s)', (key, now))
            conn.commit()
            # 节流全局清理：每 200 次调用清理一次全表过期行，防止表膨胀
            _RL_CHECK_COUNT += 1
            if _RL_CHECK_COUNT % 200 == 0:
                conn.execute('DELETE FROM shop.rate_limits WHERE ts<%s',
                             (now - window * 4,))
                conn.commit()
    except Exception as e:
        logger.warning(f'[Shop] Rate limit check failed: {e}')
        return True  # DB 异常时放行，避免阻断正常业务
    return True


def _safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _require_user():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    payload = validate_token(token) if token else None
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Please log in first')}), 401)
    return payload, None


# =============================================
# 页面渲染
# =============================================
@shop_public_bp.route('', methods=['GET'])
@shop_public_bp.route('/', methods=['GET'])
def shop_page():
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if request.args.get('token'):
        # 消费 URL 中的 token：写入 cookie 后 302 到干净 URL，避免 token 残留地址栏/访问日志
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(redirect(request.path))
        resp.set_cookie('sso_token', token, path='/', max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
        return resp
    return make_response(render_template('public/shop.html', token=token))


@shop_public_bp.route('/qr', methods=['GET'])
def shop_qr():
    """生成二维码图片（本地化，替代外部 api.qrserver.com）"""
    data = (request.args.get('data') or '').strip()
    if not data or len(data) > 4096:
        return make_response('bad request', 400)
    from io import BytesIO
    import qrcode
    img = qrcode.make(data)
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@shop_public_bp.route('/<int:pid>', methods=['GET'])
def shop_detail(pid):
    """商品详情页（普通用户访问）"""
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if request.args.get('token'):
        # 消费 URL 中的 token：写入 cookie 后 302 到干净 URL，避免 token 残留地址栏/访问日志
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(redirect(request.path))
        resp.set_cookie('sso_token', token, path='/', max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
        return resp
    return make_response(render_template('public/shop_detail.html', token=token))


@shop_public_bp.route('/preview/<int:pid>', methods=['GET'])
def shop_preview(pid):
    """商品预览页（管理员预览，绕过下架检查）"""
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if request.args.get('token'):
        # 消费 URL 中的 token：写入 cookie 后 302 到干净 URL，避免 token 残留地址栏/访问日志
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(redirect(request.path))
        resp.set_cookie('sso_token', token, path='/', max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
        return resp
    return make_response(render_template('public/shop_detail.html', token=token, preview=True))


@shop_public_bp.route('/cart', methods=['GET'])
def cart_page():
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if request.args.get('token'):
        # 消费 URL 中的 token：写入 cookie 后 302 到干净 URL，避免 token 残留地址栏/访问日志
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(redirect(request.path))
        resp.set_cookie('sso_token', token, path='/', max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
        return resp
    return make_response(render_template('public/cart.html', token=token))


@shop_public_bp.route('/pay/<oid>', methods=['GET'])
def payment_page(oid):
    """支付页 — 展示订单信息，用户点击后调支付宝"""
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if request.args.get('token'):
        # 消费 URL 中的 token：写入 cookie 后 302 到干净 URL，避免 token 残留地址栏/访问日志
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(redirect(request.path))
        resp.set_cookie('sso_token', token, path='/', max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
        return resp
    return make_response(render_template('public/payment.html', order_id=oid, token=token))


@shop_public_bp.route('/orders', methods=['GET'])
def orders_page():
    """订单列表页"""
    token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if request.args.get('token'):
        # 消费 URL 中的 token：写入 cookie 后 302 到干净 URL，避免 token 残留地址栏/访问日志
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp = make_response(redirect(request.path))
        resp.set_cookie('sso_token', token, path='/', max_age=604800, samesite='Lax', secure=_is_https, httponly=True)
        return resp
    return make_response(render_template('public/orders.html', token=token))


# =============================================
# API: 当前登录用户信息
# =============================================
@shop_public_bp.route('/api/user/info', methods=['GET'])
def api_user_info():
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        user = conn.execute(
            'SELECT id, username, display_name, phone, email, avatar_url, is_admin, created_at '
            'FROM users WHERE id=%s', (uid,)
        ).fetchone()
    if not user:
        return jsonify({'success': False, 'error': _('User not found')}), 404
    d = dict(user)
    d['is_admin'] = bool(d['is_admin'])
    return jsonify({'success': True, 'data': d})


@shop_public_bp.route('/cloud', methods=['GET'])
def cloud_instances_page():
    from flask import session
    token = session.get('token', '')
    return render_template('cloud_instances.html', token=token)


# =============================================
# API: 商品列表
# =============================================
@shop_public_bp.route('/api/products', methods=['GET'])
def api_products():
    category = request.args.get('category', '')
    search = request.args.get('search', '')
    cat_id = request.args.get('category_id', type=int, default=0)
    with get_db() as conn:
        sql = '''SELECT p.*, c.name as category_name FROM products p
                 LEFT JOIN categories c ON p.category_id=c.id
                 WHERE p.is_active=1'''
        params = []
        if category:
            sql += ' AND p.category LIKE %s'
            params.append(f'%{category}%')
        if cat_id:
            sql += ' AND p.category_id=%s'
            params.append(cat_id)
        if search:
            sql += ' AND (p.title LIKE %s OR p.subtitle LIKE %s OR p.description LIKE %s)'
            s = f'%{search}%'
            params.extend([s, s, s])
        sql += ' ORDER BY p.sort_order ASC, p.id DESC'
        rows = conn.execute(sql, params).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        # 解析JSON字段
        for f in ['features', 'images', 'ai_config']:
            if isinstance(d.get(f), str):
                try:
                    d[f] = json.loads(d[f])
                except:
                    if f == 'images':
                        d[f] = []
                    elif f == 'ai_config':
                        d[f] = {}
                    elif f == 'features':
                        d[f] = []
        data.append(d)
    return jsonify({'success': True, 'data': data})


@shop_public_bp.route('/api/products/<int:pid>', methods=['GET'])
def api_product_detail(pid):
    with get_db() as conn:
        row = conn.execute(
            '''SELECT p.*, c.name as category_name FROM products p
               LEFT JOIN categories c ON p.category_id=c.id
               WHERE p.id=%s AND p.is_active=1''', (pid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist or has been removed')}), 404
        d = dict(row)
        for f in ['features', 'images', 'ai_config']:
            if isinstance(d.get(f), str):
                try:
                    d[f] = json.loads(d[f])
                except:
                    d[f] = [] if f in ['features', 'images'] else {}
    return jsonify({'success': True, 'data': d})


@shop_public_bp.route('/api/products/<int:pid>/skus', methods=['GET'])
def api_product_skus(pid):
    """获取商品SKU（公共）"""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, sku_code, spec_path, price, stock FROM product_skus WHERE product_id=%s AND is_active=1',
            (pid,)
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@shop_public_bp.route('/api/recommend', methods=['GET'])
def api_recommend():
    """猜你喜欢 — 按收藏热度(wish_count)+销量(sales_count)推荐，排除当前商品"""
    limit = min(request.args.get('limit', 6, type=int), 20)
    exclude = request.args.get('exclude', 0, type=int)
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT p.id, p.title, p.price, p.original_price, p.thumbnail, p.sales_count
               FROM products p
               WHERE p.is_active=1 AND p.status='active' AND p.id != %s
               ORDER BY p.wish_count DESC, p.sales_count DESC, p.sort_order ASC, p.id DESC
               LIMIT %s''', (exclude, limit)
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


# =============================================
# API: 购物车
# =============================================
def _guest_cart():
    """游客购物车（session 存储）：{ 'pid:sku_id': qty }"""
    return session.get('guest_cart') or {}


def _set_guest_cart(cart):
    session['guest_cart'] = cart


def _guest_cart_response():
    """游客购物车列表 — 与登录态返回同结构，cart_id 用 'pid:sku_id' key"""
    cart = _guest_cart()
    items = []
    total = 0
    if cart:
        with get_db() as conn:
            for key, qty in cart.items():
                try:
                    pid_s, sku_s = key.split(':', 1)
                    pid, sku_id = int(pid_s), int(sku_s or 0)
                except Exception:
                    continue
                row = conn.execute(
                    '''SELECT p.id, p.title, p.subtitle, p.price, p.original_price, p.thumbnail,
                              sk.id AS sku_id, sk.sku_code, sk.spec_path, sk.price AS sku_price
                       FROM products p
                       LEFT JOIN product_skus sk ON sk.id=%s
                       WHERE p.id=%s AND p.is_active=1''', (sku_id or None, pid)
                ).fetchone()
                if not row:
                    continue
                item = dict(row)
                item['quantity'] = int(qty)
                item['subtotal'] = (item['sku_price'] or item['price']) * item['quantity']
                item['id'] = key
                total += item['subtotal']
                items.append(item)
    return jsonify({'success': True, 'data': {'items': items, 'total': round(total, 2)}})


def _merge_guest_cart(uid):
    """登录后把 session 游客购物车合并进 DB 购物车（按 SKU 去重加量），清空 session"""
    cart = _guest_cart()
    if not cart:
        return 0
    merged = 0
    with get_db() as conn:
        for key, qty in cart.items():
            try:
                pid_s, sku_s = key.split(':', 1)
                pid, sku_id = int(pid_s), int(sku_s or 0)
            except Exception:
                continue
            existing = conn.execute(
                'SELECT id, quantity FROM carts WHERE user_id=%s AND product_id=%s AND sku_id=%s',
                (uid, pid, sku_id)
            ).fetchone()
            if existing:
                new_qty = min(existing['quantity'] + int(qty), _MAX_ORDER_QTY)
                conn.execute('UPDATE carts SET quantity=%s WHERE id=%s', (new_qty, existing['id']))
            else:
                conn.execute(
                    'INSERT INTO carts (user_id, product_id, quantity, sku_id) VALUES (%s,%s,%s,%s)',
                    (uid, pid, int(qty), sku_id)
                )
            merged += 1
        conn.commit()
    session.pop('guest_cart', None)
    return merged


def _guest_add_to_cart(pid, sku_id, qty):
    """游客加购：校验商品/SKU 库存后存入 session"""
    with get_db() as conn:
        prod = conn.execute('SELECT id, stock, is_active FROM products WHERE id=%s', (pid,)).fetchone()
        if not prod:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        if not prod['is_active']:
            return jsonify({'success': False, 'error': _('Product has been removed')}), 400
        if sku_id:
            sku_info = conn.execute(
                'SELECT id, price, stock FROM product_skus WHERE id=%s AND product_id=%s AND is_active=1',
                (sku_id, pid)
            ).fetchone()
            if not sku_info:
                return jsonify({'success': False, 'error': _('SKU does not exist')}), 400
            if sku_info['stock'] < qty:
                return jsonify({'success': False, 'error': _('SKU stock insufficient')}), 400
        elif prod['stock'] is not None:
            if prod['stock'] <= 0:
                return jsonify({'success': False, 'error': _('Product is sold out')}), 400
            if prod['stock'] < qty:
                return jsonify({'success': False, 'error': _('Insufficient stock')}), 400
    cart = _guest_cart()
    key = f'{pid}:{sku_id or 0}'
    cart[key] = min(cart.get(key, 0) + qty, _MAX_ORDER_QTY)
    _set_guest_cart(cart)
    return jsonify({'success': True, 'message': _('Added to cart')})


@shop_public_bp.route('/api/cart', methods=['GET'])
def api_get_cart():
    payload, err = _require_user()
    if err:
        return _guest_cart_response()
    uid = payload['user_id']
    _merge_guest_cart(uid)
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT c.*, p.title, p.subtitle, p.price, p.original_price, p.thumbnail, p.stock, p.is_active,
                      sk.sku_code, sk.spec_path, sk.price as sku_price
               FROM carts c
               JOIN products p ON c.product_id=p.id
               LEFT JOIN product_skus sk ON c.sku_id=sk.id
               WHERE c.user_id=%s ORDER BY c.created_at DESC''', (uid,)
        ).fetchall()
        items = []
        total = 0
        for r in rows:
            item = dict(r)
            item['subtotal'] = (item['sku_price'] or item['price']) * item['quantity']
            total += item['subtotal']
            items.append(item)
    return jsonify({'success': True, 'data': {'items': items, 'total': round(total, 2)}})


@shop_public_bp.route('/api/cart/add', methods=['POST'])
def api_add_to_cart():
    data = request.get_json() or {}
    pid = _safe_int(data.get('product_id'), 0)
    qty = _safe_int(data.get('quantity', 1))
    sku_id = _safe_int(data.get('sku_id'), 0)
    if pid <= 0:
        return jsonify({'success': False, 'error': _('Missing product ID')}), 400
    if qty < 1:
        return jsonify({'success': False, 'error': _('Quantity cannot be less than 1')}), 400
    if qty > _MAX_ORDER_QTY:
        return jsonify({'success': False, 'error': _('Quantity is too large')}), 400

    payload, err = _require_user()
    if err:
        # 游客：校验后存入 session 购物车
        return _guest_add_to_cart(pid, sku_id, qty)
    uid = payload['user_id']
    if not _check_rate_limit(uid, 'cart', max_requests=60, window=60):
        return jsonify({'success': False, 'error': _('Operation too frequent')}), 429

    with get_db() as conn:
        prod = conn.execute('SELECT id, stock, is_active FROM products WHERE id=%s', (pid,)).fetchone()
        if not prod:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        if not prod['is_active']:
            return jsonify({'success': False, 'error': _('Product has been removed')}), 400

        sku_info = None
        if sku_id:
            sku_info = conn.execute(
                'SELECT id, price, stock FROM product_skus WHERE id=%s AND product_id=%s AND is_active=1',
                (sku_id, pid)
            ).fetchone()
            if not sku_info:
                return jsonify({'success': False, 'error': _('SKU does not exist')}), 400
            if sku_info['stock'] < qty:
                return jsonify({'success': False, 'error': _('SKU stock insufficient')}), 400
        elif prod['stock'] is not None:
            if prod['stock'] <= 0:
                return jsonify({'success': False, 'error': _('Product is sold out')}), 400
            if prod['stock'] < qty:
                return jsonify({'success': False, 'error': _('Insufficient stock')}), 400

        existing = conn.execute(
            'SELECT id, quantity FROM carts WHERE user_id=%s AND product_id=%s AND sku_id=%s',
            (uid, pid, sku_id)
        ).fetchone()
        if existing:
            new_qty = existing['quantity'] + qty
            if new_qty > _MAX_ORDER_QTY:
                return jsonify({'success': False, 'error': _('Quantity is too large')}), 400
            conn.execute('UPDATE carts SET quantity=%s WHERE id=%s', (new_qty, existing['id']))
        else:
            conn.execute(
                'INSERT INTO carts (user_id, product_id, quantity, sku_id) VALUES (%s,%s,%s,%s)',
                (uid, pid, qty, sku_id)
            )
        conn.commit()
    return jsonify({'success': True, 'message': _('Added to cart')})


@shop_public_bp.route('/api/cart/update', methods=['POST'])
def api_update_cart():
    data = request.get_json() or {}
    cid = data.get('cart_id')
    qty = _safe_int(data.get('quantity', 1))
    if qty < 1:
        return jsonify({'success': False, 'error': _('Quantity cannot be less than 1')}), 400
    if qty > _MAX_ORDER_QTY:
        return jsonify({'success': False, 'error': _('Quantity is too large')}), 400
    payload, err = _require_user()
    if err:
        # 游客：更新 session 购物车数量
        cart = _guest_cart()
        if cid in cart:
            cart[cid] = qty
            _set_guest_cart(cart)
        return jsonify({'success': True, 'message': _('Updated')})
    uid = payload['user_id']
    with get_db() as conn:
        conn.execute('UPDATE carts SET quantity=%s WHERE id=%s AND user_id=%s', (qty, cid, uid))
        conn.commit()
    return jsonify({'success': True, 'message': _('Updated')})


@shop_public_bp.route('/api/cart/remove', methods=['POST'])
def api_remove_from_cart():
    data = request.get_json() or {}
    cid = data.get('cart_id')
    payload, err = _require_user()
    if err:
        # 游客：从 session 购物车删除
        cart = _guest_cart()
        if cid in cart:
            cart.pop(cid, None)
            _set_guest_cart(cart)
        return jsonify({'success': True, 'message': _('Removed')})
    uid = payload['user_id']
    with get_db() as conn:
        conn.execute('DELETE FROM carts WHERE id=%s AND user_id=%s', (cid, uid))
        conn.commit()
    return jsonify({'success': True, 'message': _('Removed')})


# =============================================
# API: 用户地址列表
# =============================================
@shop_public_bp.route('/api/addresses', methods=['GET'])
def api_addresses():
    """Return user's saved addresses (cn + intl)"""
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        cn_rows = conn.execute(
            'SELECT id, recipient_name, phone, province_code, city_code, district_code, street_code, street_address, postal_code, is_default FROM user_addresses WHERE user_id=%s AND status=1',
            (uid,)
        ).fetchall()
        intl_rows = conn.execute(
            'SELECT id, recipient_name, phone, country, state, city, address_line1, address_line2, postal_code, is_default FROM user_addresses_intl WHERE user_id=%s AND status=1',
            (uid,)
        ).fetchall()
    cn_list = []
    for r in cn_rows:
        parts = [r['province_code'], r['city_code'], r['district_code'], r['street_code'], r['street_address']]
        addr = ' '.join(p for p in parts if p)
        cn_list.append({'id': r['id'], 'type': 'cn', 'recipient_name': r['recipient_name'], 'phone': r['phone'], 'address': addr, 'is_default': bool(r['is_default'])})
    intl_list = []
    for r in intl_rows:
        parts = [r['country'], r['state'], r['city'], r['address_line1'], r['address_line2']]
        addr = ', '.join(p for p in parts if p)
        intl_list.append({'id': r['id'], 'type': 'intl', 'recipient_name': r['recipient_name'], 'phone': r['phone'], 'address': addr, 'is_default': bool(r['is_default'])})
    all_addrs = cn_list + intl_list
    default = None
    for a in all_addrs:
        if a['is_default']:
            default = a
            break
    return jsonify({'success': True, 'data': {'addresses': all_addrs, 'default': default}})


# =============================================
# API: 下单
# =============================================
@shop_public_bp.route('/api/checkout', methods=['POST'])
def api_checkout():
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    if not _check_rate_limit(uid, 'checkout', max_requests=10, window=60):
        return jsonify({'success': False, 'error': _('Operation too frequent, please try again later')}), 429
    data = request.get_json() or {}
    idempotency_key = (data.get('idempotency_key', '') or '').strip()

    # 幂等检查
    if idempotency_key:
        with get_db() as conn:
            existing = conn.execute(
                'SELECT order_id, total, status FROM order_items WHERE idempotency_key=%s LIMIT 1',
                (idempotency_key,)
            ).fetchone()
            if existing:
                return jsonify({
                    'success': True,
                    'data': {
                        'order_id': existing['order_id'],
                        'total': existing['total'],
                        'duplicate': True,
                        'note': _('This order already exists, returning the existing result')
                    }
                })

    # 用户只能传 product_id + quantity（不允许传 price）
    raw_items = data.get('items', [])
    coupon_code = data.get('coupon_code', '').strip().upper()

    # ── Resolve shipping address ──
    address_id = data.get('address_id')
    address_type = data.get('address_type', 'cn')  # 'cn' or 'intl'
    receiver_name = ''
    receiver_phone = ''
    receiver_address = ''

    if address_id:
        with get_db() as conn:
            if address_type == 'intl':
                addr = conn.execute(
                    'SELECT * FROM user_addresses_intl WHERE id=%s AND user_id=%s',
                    (address_id, uid)
                ).fetchone()
                if addr:
                    receiver_name = addr['recipient_name']
                    receiver_phone = addr['phone']
                    parts = [addr['country'], addr['state'], addr['city'],
                             addr['address_line1'], addr.get('address_line2', '')]
                    receiver_address = ', '.join(p for p in parts if p)
            else:
                addr = conn.execute(
                    'SELECT * FROM user_addresses WHERE id=%s AND user_id=%s',
                    (address_id, uid)
                ).fetchone()
                if addr:
                    receiver_name = addr['recipient_name']
                    receiver_phone = addr['phone']
                    parts = [addr.get('province_code',''), addr.get('city_code',''),
                             addr.get('district_code',''), addr.get('street_code',''),
                             addr['street_address']]
                    receiver_address = ' '.join(p for p in parts if p)

    items = []

    with get_db() as conn:
        if not raw_items:
            # 从购物车取
            cart_rows = conn.execute(
                '''SELECT c.product_id, c.quantity, c.sku_id, p.title, p.is_active,
                          COALESCE(sk.price, p.price) AS price, COALESCE(sk.stock, p.stock) AS stock
                   FROM carts c JOIN products p ON c.product_id=p.id
                   LEFT JOIN product_skus sk ON sk.id=c.sku_id AND sk.is_active=1
                   WHERE c.user_id=%s''', (uid,)
            ).fetchall()
            if not cart_rows:
                return jsonify({'success': False, 'error': _('Cart is empty')}), 400
            for r in cart_rows:
                if not r['is_active']:
                    continue
                items.append({'product_id': r['product_id'], 'quantity': r['quantity'],
                              'sku_id': r['sku_id'] or 0, 'title': r['title'], 'price': r['price']})
        else:
            # 从用户传入 — 价格必须从数据库查，不接受客户端价格
            for item in raw_items:
                pid = _safe_int(item.get('product_id'), 0)
                qty = _safe_int(item.get('quantity', 1))
                sku_id = _safe_int(item.get('sku_id'), 0)
                if pid <= 0:
                    return jsonify({'success': False, 'error': _('Missing product ID')}), 400
                if qty < 1:
                    return jsonify({'success': False, 'error': _('Quantity cannot be less than 1')}), 400
                if qty > _MAX_ORDER_QTY:
                    return jsonify({'success': False, 'error': _('Quantity is too large')}), 400
                prod = conn.execute(
                    'SELECT id, title, price, stock, is_active FROM products WHERE id=%s', (pid,)
                ).fetchone()
                if not prod or not prod['is_active']:
                    return jsonify({'success': False, 'error': f"{_('Product does not exist or has been removed')}: {pid}"}), 400
                price = prod['price']
                if sku_id:
                    sku = conn.execute(
                        'SELECT id, price, stock FROM product_skus WHERE id=%s AND product_id=%s AND is_active=1',
                        (sku_id, pid)
                    ).fetchone()
                    if not sku:
                        return jsonify({'success': False, 'error': _('SKU does not exist')}), 400
                    if sku['stock'] < qty:
                        return jsonify({'success': False, 'error': _('SKU stock insufficient')}), 400
                    price = sku['price']
                items.append({'product_id': prod['id'], 'quantity': qty,
                              'sku_id': sku_id, 'title': prod['title'], 'price': price})

    if not items:
        return jsonify({'success': False, 'error': _('No valid products')}), 400

    # 计算总价 — 价格全部来自数据库
    subtotal = sum(float(item['price']) * int(item['quantity']) for item in items)
    total = round(subtotal, 2)
    order_id = 'SP' + datetime.now().strftime('%Y%m%d%H%M%S') + secrets.token_hex(4).upper()

    # ── 单事务：优惠券验证 + 订单创建 + 使用计数 + 清空购物车 ──
    with get_db() as conn:
        discount = 0
        coupon_id = None

        if coupon_code:
            # VR-SHOP-001：coupons 表在独立 coupons schema，主库 search_path 不含，
            # 需用 schema 限定名，否则用券必「relation coupons does not exist」
            cpn = conn.execute(
                'SELECT * FROM coupons.coupons WHERE code=%s AND is_active=1', (coupon_code,)
            ).fetchone()
            if not cpn:
                return jsonify({'success': False, 'error': _('Coupon is invalid')}), 400
            if cpn['usage_limit'] and cpn['used_count'] >= cpn['usage_limit']:
                return jsonify({'success': False, 'error': _('Coupon is used up')}), 400
            if subtotal < cpn['min_amount']:
                return jsonify({'success': False, 'error': f"{_('Minimum spend not reached')} ¥{cpn['min_amount']}"}), 400
            if cpn['expire_at'] and cpn['expire_at'] < datetime.now().isoformat():
                return jsonify({'success': False, 'error': _('Coupon has expired')}), 400
            coupon_id = cpn['id']
            if cpn['coupon_type'] == 'fixed':
                discount = min(cpn['value'], subtotal)
            else:
                discount = round(subtotal * cpn['value'] / 100, 2)
            # ← used_count+1 在事务内，下行插入后提交
            conn.execute('UPDATE coupons.coupons SET used_count=used_count+1 WHERE id=%s', (coupon_id,))

        total = round(subtotal - discount, 2)

        for item in items:
            _sku_id = int(item.get('sku_id', 0) or 0)
            _qty = int(item.get('quantity', 1))
            _unit = float(item.get('price', 0))
            conn.execute(
                '''INSERT INTO order_items (order_id, user_id, product_id, product_title,
                   quantity, unit_price, sku_id, sku_price, subtotal, coupon_id, discount, status, idempotency_key, created_at,
                   receiver_name, receiver_phone, receiver_address, total)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s,%s)''',
                (order_id, uid, item['product_id'], (item.get('title', '') or '')[:200],
                 _qty, _unit, _sku_id, _unit if _sku_id else 0,
                 round(_unit * _qty, 2),
                 coupon_id, round(discount / max(len(items), 1), 2) if coupon_id else 0,
                 'pending', idempotency_key,
                 receiver_name, receiver_phone, receiver_address, total)
            )
            # 增加销量 + 扣减库存（含 SKU 库存）
            # VR-SHOP-001：PostgreSQL 无标量 MAX，库存钳位用 GREATEST
            conn.execute('UPDATE products SET sales_count=sales_count+%s, stock=GREATEST(0,stock-%s) WHERE id=%s',
                         (_qty, _qty, item['product_id']))
            if _sku_id:
                conn.execute('UPDATE product_skus SET stock=GREATEST(0,stock-%s) WHERE id=%s',
                             (_qty, _sku_id))
        # 清空购物车
        if not data.get('keep_cart'):
            conn.execute('DELETE FROM carts WHERE user_id=%s', (uid,))
        conn.commit()

    # 触发事件：订单创建
    get_event_bus().emit(EventName.ORDER_CREATED, order_id=order_id, user_id=uid,
                         total=total, items=items)

    return jsonify({
        'success': True,
        'data': {
            'order_id': order_id,
            'total': total,
            'subtotal': subtotal,
            'discount': discount,
            'items_count': len(items),
            'stub': True,
            'payment_required': True,
            'note': _('Order created, please go to orders page to complete payment')
        }
    })


# =============================================
# API: 订单查询
# =============================================
@shop_public_bp.route('/api/orders', methods=['GET'])
def api_orders():
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    page = request.args.get('page', 1, type=int) or 1
    page_size = request.args.get('page_size', 10, type=int) or 10
    page_size = min(max(page_size, 1), 50)
    offset = (page - 1) * page_size
    with get_db() as conn:
        # 订单级分页：COUNT DISTINCT order_id；分页取整单数据，避免同单多行被拆页
        total = conn.execute(
            '''SELECT COUNT(DISTINCT order_id) AS c FROM order_items
               WHERE user_id=%s AND user_deleted=0''', (uid,)
        ).fetchone()['c']
        rows = conn.execute(
            '''SELECT * FROM order_items
               WHERE user_id=%s AND user_deleted=0
                 AND order_id IN (
                     SELECT order_id FROM order_items
                     WHERE user_id=%s AND user_deleted=0
                     GROUP BY order_id
                     ORDER BY MAX(created_at) DESC
                     LIMIT %s OFFSET %s
                 )
               ORDER BY created_at DESC''',
            (uid, uid, page_size, offset)
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows],
                    'total': total, 'page': page, 'page_size': page_size})


@shop_public_bp.route('/api/orders/<oid>/delete', methods=['POST'])
def api_delete_order(oid):
    """用户删除订单（软删，仅隐藏无效订单）"""
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        order = conn.execute(
            'SELECT * FROM order_items WHERE order_id=%s AND user_id=%s',
            (oid, uid)).fetchone()
        if not order:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        allowed = ('cancelled', 'pending', 'refunded')
        if order['status'] not in allowed:
            return jsonify({'success': False, 'error': f"{_('Order cannot be deleted in current status')}: {order['status']}"}), 400
        conn.execute(
            'UPDATE order_items SET user_deleted=1 WHERE order_id=%s AND user_id=%s',
            (oid, uid))
        conn.commit()
    return jsonify({'success': True, 'message': _('Deleted')})


@shop_public_bp.route('/api/orders/<oid>/cancel', methods=['POST'])
def api_cancel_order(oid):
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM order_items WHERE order_id=%s AND user_id=%s', (oid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] != 'pending':
            return jsonify({'success': False, 'error': _('Only pending payment orders can be cancelled')}), 400
        # 同一事务：先回补商品/SKU 库存，再置为已取消（仅本次 pending 行，避免重复回补）
        _cancel_rows = conn.execute(
            'SELECT product_id, sku_id, quantity FROM order_items WHERE order_id=%s AND status=%s',
            (oid, 'pending')
        ).fetchall()
        for _r in _cancel_rows:
            conn.execute('UPDATE products SET stock=stock+%s WHERE id=%s',
                         (_r['quantity'], _r['product_id']))
            if _r['sku_id']:
                conn.execute('UPDATE product_skus SET stock=stock+%s WHERE id=%s',
                             (_r['quantity'], _r['sku_id']))
        conn.execute("UPDATE order_items SET status='cancelled' WHERE order_id=%s", (oid,))
        conn.commit()
    get_event_bus().emit(EventName.ORDER_CANCELLED, order_id=oid, user_id=uid)
    return jsonify({'success': True, 'message': _('Cancelled')})


@shop_public_bp.route('/api/orders/<oid>/confirm-receipt', methods=['POST'])
def api_confirm_receipt(oid):
    """用户确认收货 → 标记为已完成"""
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    from datetime import datetime
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM order_items WHERE order_id=%s AND user_id=%s', (oid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] != 'paid':
            return jsonify({'success': False, 'error': _('Only paid orders can be confirmed as received')}), 400
        if row['shipping_status'] != 'shipped':
            return jsonify({'success': False, 'error': _('Order has not been shipped yet')}), 400
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE order_items SET status='completed', completed_at=%s WHERE order_id=%s",
            (now, oid)
        )
        conn.commit()
    get_event_bus().emit('order.completed', order_id=oid, user_id=uid)
    return jsonify({'success': True, 'message': _('Confirmed as received')})


@shop_public_bp.route('/api/orders/<oid>/request-refund', methods=['POST'])
def api_request_refund(oid):
    """用户申请退款"""
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'success': False, 'error': _('Please provide a refund reason')}), 400
    from datetime import datetime
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM order_items WHERE order_id=%s AND user_id=%s', (oid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] not in ('paid',):
            return jsonify({'success': False, 'error': _('Current order status does not allow refund request')}), 400
        if row['refund_reason']:
            return jsonify({'success': False, 'error': _('Refund requested, please wait for processing')}), 400
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE order_items SET status='refunding', refund_reason=%s, refund_requested_at=%s WHERE order_id=%s",
            (reason, now, oid)
        )
        conn.commit()
    get_event_bus().emit(EventName.ORDER_REFUNDED, order_id=oid, user_id=uid, reason=reason)
    return jsonify({'success': True, 'message': _('Refund request submitted')})


@shop_public_bp.route('/orders/<int:oid>/track-user', methods=['GET'])
def track_order_user(oid):
    """用户端查询物流轨迹"""
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        row = conn.execute(
            'SELECT oi.*, ec.kdniao_code FROM order_items oi '
            'LEFT JOIN express_companies ec ON oi.tracking_company=ec.code '
            'WHERE oi.id=%s AND oi.user_id=%s', (oid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if not row.get('tracking_number'):
            return jsonify({'success': True, 'data': {
                'tracking_company': '', 'tracking_number': '',
                'shipped_at': '', 'shipping_status': row.get('shipping_status', ''),
                'traces': [],
            }})
        shipper_code = row['kdniao_code'] or row['tracking_company']
        logistic_code = row['tracking_number']

    success, data, err_msg = False, {}, _('Logistics plugin is not enabled')
    _logistics = _get_plugin_instance('logistics')
    if _logistics:
        success, data, err_msg = _logistics.query_track(shipper_code, logistic_code)
    return jsonify({
        'success': True,
        'data': {
            'tracking_company': row['tracking_company'],
            'tracking_number': row['tracking_number'],
            'shipped_at': row.get('shipped_at', ''),
            'shipping_status': row.get('shipping_status', ''),
            'traces': data.get('traces', []) if success else [],
            'state_text': data.get('state_text', '') if success else '',
            'track_error': err_msg if not success else '',
        }
    })


# =============================================
# API: 发起支付
# =============================================
@shop_public_bp.route('/api/pay/<oid>', methods=['POST'])
def api_pay_order(oid):
    """为订单创建支付（支持支付宝/微信）"""
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']
    with get_db() as conn:
        items = conn.execute(
            'SELECT * FROM order_items WHERE order_id=%s AND user_id=%s',
            (oid, uid)
        ).fetchall()
        if not items:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if items[0]['status'] != 'pending':
            return jsonify({'success': False, 'error': _('Current order status does not allow payment')}), 400
        total = round(sum(
            (float(r['subtotal']) or 0) - (float(r['discount']) or 0)
            for r in items
        ), 2)
        subject = items[0]['product_title'][:64] if items else _('Mall Order')

    method = (request.get_json() or {}).get('method', 'alipay')

    if method == 'wechat':
        # ── 微信支付 ──
        from plugins.payment.gateways.wechat import call_native_pay
        notify_base = os.environ.get('NOTIFY_BASE', '')
        if not notify_base:
            try:
                with get_db() as pgconn:
                    row = pgconn.execute(
                        "SELECT value FROM system_config WHERE key=%s",
                        ('payment.notify_base',)
                    ).fetchone()
                    if row:
                        notify_base = row[0]
            except Exception:
                pass
        shop_notify_url = notify_base.rstrip('/') + '/shop/api/pay/wechat-notify' if notify_base else ''
        result = call_native_pay(oid, subject, int(round(total * 100)), notify_url=shop_notify_url)
        # 失败或未配置（fail-closed，不再 mock 假成功）
        return jsonify({'success': not result.get('error') and not result.get('stub'),
                        'data': {'method': 'wechat', **result}})

    # ── 支付宝（默认） ──
    # 支付业务统一走 auth-center 业务层（payment 插件仅提供网关能力，不含商城业务）
    try:
        from services.payment_service import create_shop_payment
        result = create_shop_payment(oid, total, subject)
    except ImportError:
        return jsonify({'success': False, 'error': _('Payment service not ready')}), 500

    # 获取用户 token，写入 cookie 确保支付后跳回时登录态保持
    auth = request.headers.get('Authorization', '')
    user_token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not user_token:
        user_token = request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''

    resp = jsonify({'success': result.get('success', False), 'data': {'method': 'alipay', **result}})
    if user_token:
        _is_https = os.environ.get('DEPLOY_PROTOCOL', 'https') == 'https'
        resp.set_cookie('sso_token', user_token, path='/', max_age=604800,
                        samesite='Lax', secure=_is_https, httponly=True)
    return resp


# =============================================
# API: 桩模式确认支付（开发/测试用）
# =============================================
@shop_public_bp.route('/api/pay/<oid>/stub-confirm', methods=['POST'])
def api_stub_confirm(oid):
    """Dev-mode only: mark a shop order as paid without going through a gateway."""
    if os.environ.get('DEPLOY_ENV', '') != 'dev':
        return jsonify({'success': False, 'error': _('stub confirm is disabled outside dev')}), 403
    payload, err = _require_user()
    if err:
        return err
    uid = payload['user_id']

    # 检查当前状态
    with get_db() as conn:
        row = conn.execute(
            'SELECT status FROM order_items WHERE order_id=%s AND user_id=%s', (oid, uid)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] == 'paid':
            return jsonify({'success': True, 'message': _('Order paid')})
        if row['status'] != 'pending':
            return jsonify({'success': False, 'error': _('Order status does not allow payment')}), 400

    # 商城支付业务走 auth-center 业务层（payment 插件仅提供网关能力）
    from services.payment_service import confirm_shop_order
    success, msg = confirm_shop_order(oid, f'STUB_{oid}', 'stub')
    return jsonify({'success': success, 'message': msg})


# =============================================
# API: 支付回调（微信异步通知）
# =============================================
@shop_public_bp.route('/api/pay/wechat-notify', methods=['POST'])
def api_wechat_notify():
    """微信支付异步通知回调 — 商城订单"""
    from plugins.payment.gateways.wechat import _verify_wechat_sign, _decrypt_wechat_resource
    # 商城支付业务走 auth-center 业务层（payment 插件仅提供网关能力）
    from services.payment_service import confirm_shop_order as confirm_fn

    body = request.get_data(as_text=True)
    headers = request.headers

    # 验签
    if not _verify_wechat_sign(headers, body):
        return 'FAIL', 400

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return 'FAIL', 400

    resource = data.get('resource', {})
    resource_plain = _decrypt_wechat_resource(resource)
    if not resource_plain:
        import logging
        logging.error("微信支付回调（商城）解密失败")
        return 'FAIL', 400

    trade_state = resource_plain.get('trade_state', '')
    if trade_state != 'SUCCESS':
        return 'FAIL', 400

    order_id = resource_plain.get('out_trade_no', '')
    transaction_id = resource_plain.get('transaction_id', '')

    if not order_id:
        return 'FAIL', 400

    success, msg = confirm_fn(order_id)
    return 'SUCCESS' if success else 'FAIL', 200 if success else 400


# =============================================
# API: 支付回调（支付宝异步通知）
# =============================================
@shop_public_bp.route('/api/pay/notify', methods=['POST'])
def api_pay_notify():
    """支付宝异步通知回调"""
    # 商城支付业务走 auth-center 业务层（payment 插件仅提供网关能力）
    from services.payment_service import verify_notify as verify_fn, confirm_shop_order as confirm_fn
    data = request.form.to_dict()
    trade_status = data.get('trade_status', '')
    order_id = data.get('out_trade_no', '')
    trade_no = data.get('trade_no', '')
    if trade_status != 'TRADE_SUCCESS':
        return 'failure'
    if not verify_fn(data):
        return 'failure'
    success, msg = confirm_fn(order_id)
    return 'success' if success else 'failure'


# =============================================
# API: 查询支付状态
# =============================================
@shop_public_bp.route('/api/pay/status/<oid>', methods=['GET'])
def api_pay_status(oid):
    """查询订单支付状态"""
    payload, err = _require_user()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute(
            'SELECT status, paid_at, payment_method, payment_trade_no FROM order_items WHERE order_id=%s',
            (oid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
    return jsonify({
        'success': True,
        'data': {
            'status': row['status'],
            'paid_at': row.get('paid_at', ''),
            'payment_method': row.get('payment_method', ''),
        }
    })


# =============================================
# API: 优惠券验证（已迁移至插件: plugins/coupons/）
# =============================================
@shop_public_bp.route('/api/coupon/validate', methods=['POST'])
def api_validate_coupon():
    """桥接到插件引擎"""
    try:
        from plugins.coupons import get_engine
        engine = get_engine()
        if engine:
            payload, err = _require_user()
            if err:
                return err
            uid = payload['user_id']
            if not _check_rate_limit(uid, 'coupon', max_requests=30, window=60):
                return jsonify({'success': False, 'error': _('Operation too frequent')}), 429
            data = request.get_json() or {}
            result = engine.validate(
                code=data.get('code', '').strip().upper(),
                amount=_safe_float(data.get('amount', 0)),
                user_id=uid,
                quantity=_safe_int(data.get('quantity', 0)),
                product_id=data.get('product_id'),
            )
            if not result['valid']:
                return jsonify({'success': False, 'error': result['error']}), 400
            cpn = result['coupon']
            return jsonify({
                'success': True,
                'data': {
                    'id': cpn['id'],
                    'code': cpn['code'],
                    'name': cpn.get('name', cpn['code']),
                    'coupon_type': cpn['coupon_type'],
                    'coupon_category': cpn.get('coupon_category', 'general'),
                    'value': cpn['value'],
                    'discount': result['discount']
                }
            })
    except Exception:
        pass
    return jsonify({'success': False, 'error': _('Coupon service unavailable')}), 503
