#!/usr/bin/env python3
"""
Subscription Plugin — API 路由
================================
用户端:  /plugin/subscription/api/*
管理端:  /plugin/subscription/admin/*
回调:    /plugin/subscription/api/notify/*
门户:    /plugin/subscription/portal
"""

import os
from flask import Blueprint, request, jsonify, render_template, send_file

from .services import (
    get_subscription_service,
    has_subscription, get_active_features,
)

# ── i18n ─────────────────────────────────────────────────────────────────

_t = lambda text, **kwargs: text


def init_i18n(t_func):
    global _t
    _t = t_func


# ── Blueprint ────────────────────────────────────────────────────────────

sub_bp = Blueprint('sub_plugin', __name__, url_prefix='/plugin/subscription')


# ── 辅助 ─────────────────────────────────────────────────────────────────

def _get_user_id():
    """从 JWT/session 获取当前用户 ID"""
    # 尝试从 request 属性获取（由 auth middleware 注入）
    uid = getattr(request, 'user_id', None)
    if uid:
        return uid
    # M3: 仅保留 Cookie 和 Header，移除 ?token= query 参数通道（JWT 不再经 URL 传参）
    token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    if token:
        try:
            from services.jwt_service import validate_token
            payload = validate_token(token)
            return payload.get('user_id') or payload.get('sub')
        except Exception:
            pass
    # 尝试从 Authorization header 解析
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        try:
            from services.jwt_service import validate_token
            payload = validate_token(auth[7:])
            return payload.get('user_id') or payload.get('sub')
        except Exception:
            pass
    return None


def _login_required(f):
    """简易登录检查装饰器"""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        uid = _get_user_id()
        if not uid:
            return jsonify({'error': _t('Authentication required'), 'code': 'AUTH_REQUIRED'}), 401
        return f(*args, **kwargs)
    return wrapper


def _admin_required(f):
    """管理员检查装饰器"""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        uid = _get_user_id()
        if not uid:
            return jsonify({'error': _t('Authentication required'), 'code': 'AUTH_REQUIRED'}), 401
        role = getattr(request, 'user_role', '')
        if role == 'admin':
            return f(*args, **kwargs)
        # Fallback: check JWT is_admin claim (admin panel :8084)
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else \
            (request.cookies.get('sso_token') or request.cookies.get('tm_token'))
        if token:
            try:
                from services.jwt_service import validate_token
                payload = validate_token(token)
                if payload.get('is_admin'):
                    return f(*args, **kwargs)
            except Exception:
                pass
        return jsonify({'error': _t('Admin required'), 'code': 'FORBIDDEN'}), 403
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════
# 用户端 API
# ═══════════════════════════════════════════════════════════════════════════

@sub_bp.route('/api/items', methods=['GET'])
def list_items():
    """获取所有可订阅项（公开接口）"""
    svc = get_subscription_service()
    locale = request.args.get('lang', os.environ.get('DEPLOY_LANG', 'zh-CN'))
    items = svc.list_items(locale=locale)

    # 如果用户已登录，附带订阅状态
    uid = _get_user_id()
    if uid:
        active = get_active_features(uid)
        for item in items:
            item['subscribed'] = item['item_key'] in active

    return jsonify({'items': items, 'market': os.environ.get('DEPLOY_MARKET', 'cn')})


@sub_bp.route('/api/my', methods=['GET'])
@_login_required
def my_subscriptions():
    """我的订阅列表"""
    uid = _get_user_id()
    svc = get_subscription_service()
    subs = svc.get_user_subscriptions(uid)
    locale = request.args.get('lang', os.environ.get('DEPLOY_LANG', 'zh-CN'))

    result = []
    for s in subs:
        item = svc.get_item(s.item_key)
        d = s.to_dict()
        if item:
            d['name'] = item.name_zh if locale == 'zh-CN' else item.name_en
            d['category'] = item.category
        result.append(d)

    return jsonify({'subscriptions': result})


@sub_bp.route('/api/check/<item_key>', methods=['GET'])
@_login_required
def check_subscription(item_key):
    """检查是否有某个订阅"""
    uid = _get_user_id()
    ok = has_subscription(uid, item_key)
    return jsonify({'has_subscription': ok, 'item_key': item_key})


@sub_bp.route('/api/subscribe', methods=['POST'])
@_login_required
def subscribe():
    """创建订阅订单"""
    uid = _get_user_id()
    data = request.get_json(silent=True) or {}

    item_key = data.get('item_key', '')
    interval_type = data.get('interval_type', 'month')
    channel = data.get('channel', None)

    if not item_key:
        return jsonify({'error': _t('item_key is required'), 'code': 'INVALID_PARAMS'}), 400
    if interval_type not in ('month', 'year'):
        return jsonify({'error': _t('interval_type must be month or year'), 'code': 'INVALID_PARAMS'}), 400

    svc = get_subscription_service()
    success, msg, order_data = svc.subscribe(uid, item_key, interval_type, channel)

    if not success:
        return jsonify({'error': msg, 'code': 'SUBSCRIPTION_FAILED'}), 400

    return jsonify({'message': msg, 'order': order_data})


@sub_bp.route('/api/cancel', methods=['POST'])
@_login_required
def cancel_subscription():
    """取消订阅"""
    uid = _get_user_id()
    data = request.get_json(silent=True) or {}

    item_key = data.get('item_key', '')
    immediate = data.get('immediate', False)

    if not item_key:
        return jsonify({'error': _t('item_key is required'), 'code': 'INVALID_PARAMS'}), 400

    svc = get_subscription_service()
    success, msg = svc.cancel(uid, item_key, immediate)

    if not success:
        return jsonify({'error': msg, 'code': 'CANCEL_FAILED'}), 400

    return jsonify({'message': msg})


@sub_bp.route('/api/renew', methods=['POST'])
@_login_required
def renew_subscription():
    """手动续费"""
    uid = _get_user_id()
    data = request.get_json(silent=True) or {}

    item_key = data.get('item_key', '')
    channel = data.get('channel', None)

    if not item_key:
        return jsonify({'error': _t('item_key is required'), 'code': 'INVALID_PARAMS'}), 400

    svc = get_subscription_service()
    success, msg, order_data = svc.renew(uid, item_key, channel)

    if not success:
        return jsonify({'error': msg, 'code': 'RENEW_FAILED'}), 400

    return jsonify({'message': msg, 'order': order_data})


@sub_bp.route('/api/orders', methods=['GET'])
@_login_required
def my_orders():
    """我的订单列表"""
    uid = _get_user_id()
    limit = request.args.get('limit', 50, type=int)
    svc = get_subscription_service()
    orders = svc.list_orders(uid, limit)
    return jsonify({'orders': [o.to_dict() for o in orders]})


@sub_bp.route('/api/orders/<order_no>/retry', methods=['POST'])
@_login_required
def retry_payment(order_no):
    """重试支付：对 pending 订单重新发起支付"""
    uid = _get_user_id()
    svc = get_subscription_service()
    order = svc.get_order(order_no)
    if not order or order.user_id != uid:
        return jsonify({'error': _t('Order not found'), 'code': 'NOT_FOUND'}), 404

    success, msg, order_data = svc.retry_payment(order_no)
    if not success:
        return jsonify({'error': msg, 'code': 'RETRY_FAILED'}), 400
    return jsonify({'message': msg, 'order': order_data})


@sub_bp.route('/api/my/invoices', methods=['GET'])
@_login_required
def my_invoices():
    """我的发票列表（sub_invoices）"""
    uid = _get_user_id()
    limit = request.args.get('limit', 20, type=int)
    svc = get_subscription_service()
    return jsonify({'invoices': svc.list_invoices(uid, limit)})


@sub_bp.route('/api/my/invoices/<invoice_no>/download', methods=['GET'])
@_login_required
def download_invoice(invoice_no):
    """下载发票 PDF（data/invoices/ 目录）"""
    uid = _get_user_id()
    svc = get_subscription_service()
    inv = svc.get_invoice(uid, invoice_no)
    if not inv:
        return jsonify({'error': _t('Invoice Not Found')}), 404

    pdf_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'data', 'invoices', inv.get('pdf_path', '') or '')
    if pdf_path and os.path.exists(pdf_path):
        return send_file(pdf_path, mimetype='application/pdf', as_attachment=True,
                         download_name=f'{invoice_no}.pdf')
    # PDF 未生成（fpdf2 未安装等），返回 JSON 数据
    return jsonify({
        'invoice_no': inv['invoice_no'],
        'amount': f"¥{inv.get('amount_yuan', 0):.2f}",
        'plan_name': inv.get('plan_name', ''),
        'date': inv.get('created_at', ''),
        'status': inv.get('status', ''),
        'note': 'PDF file not available, install fpdf2 to enable PDF generation',
    })


# ═══════════════════════════════════════════════════════════════════════════
# 支付回调
# ═══════════════════════════════════════════════════════════════════════════

@sub_bp.route('/api/notify/alipay', methods=['POST'])
def notify_alipay():
    """支付宝异步通知"""
    raw_data = dict(request.form) if request.form else request.get_json(silent=True) or {}
    from plugins.payment.gateways import verify_notify
    is_valid, parsed = verify_notify('alipay', raw_data, dict(request.headers))

    if not is_valid:
        return 'FAIL', 400

    order_no = parsed.get('order_no', '')
    trade_no = parsed.get('trade_no', '')

    if order_no:
        svc = get_subscription_service()
        svc.on_payment_success(order_no, trade_no, parsed=parsed)

    return 'SUCCESS'


@sub_bp.route('/api/notify/wechat', methods=['POST'])
def notify_wechat():
    """微信支付异步通知"""
    raw_data = {}
    if request.data:
        try:
            from defusedxml.ElementTree import fromstring as safe_fromstring
            root = safe_fromstring(request.data.decode())
            raw_data = {child.tag: child.text for child in root}
        except ImportError:
            # M4: defusedxml 未安装时的降级保护（64KB 硬限制防 XML 实体展开 DoS）
            import xml.etree.ElementTree as ET
            body_text = request.data.decode()
            if len(body_text) > 65536:  # 64KB 硬限制
                return '<xml><return_code><![CDATA[FAIL]]></return_code></xml>'
            root = ET.fromstring(body_text)
            raw_data = {child.tag: child.text for child in root}
        except Exception:
            raw_data = request.get_json(silent=True) or {}

    from plugins.payment.gateways import verify_notify
    is_valid, parsed = verify_notify('wechat', raw_data, dict(request.headers))

    if not is_valid:
        return '<xml><return_code><![CDATA[FAIL]]></return_code></xml>'

    order_no = parsed.get('order_no', '')
    trade_no = parsed.get('trade_no', '')

    if order_no:
        svc = get_subscription_service()
        svc.on_payment_success(order_no, trade_no, parsed=parsed)

    return '<xml><return_code><![CDATA[SUCCESS]]></return_code></xml>'


@sub_bp.route('/api/notify/stripe', methods=['POST'])
def notify_stripe():
    """Stripe Webhook"""
    raw_data = request.get_json(silent=True) or {}
    raw_data['_raw_payload'] = request.data.decode() if request.data else ''

    from plugins.payment.gateways import verify_notify
    is_valid, parsed = verify_notify('stripe', raw_data, dict(request.headers))

    if not is_valid:
        return jsonify({'error': _t('Invalid webhook')}), 400

    order_no = parsed.get('order_no', '')
    trade_no = parsed.get('trade_no', '')

    if order_no:
        svc = get_subscription_service()
        svc.on_payment_success(order_no, trade_no, parsed=parsed)

    return jsonify({'received': True})


@sub_bp.route('/api/notify/paypal', methods=['POST'])
def notify_paypal():
    """PayPal Webhook"""
    raw_body = request.data                           # C1: 传递原始请求体供官方验签
    raw_data = request.get_json(silent=True) or {}

    from plugins.payment.gateways import verify_notify
    is_valid, parsed = verify_notify(
        'paypal', raw_data, dict(request.headers),
        raw_body=raw_body
    )

    if not is_valid:
        return jsonify({'error': _t('Invalid webhook')}), 400

    order_no = parsed.get('order_no', '')
    trade_no = parsed.get('trade_no', '')

    if order_no:
        svc = get_subscription_service()
        svc.on_payment_success(order_no, trade_no, parsed=parsed)

    return jsonify({'received': True})


# ═══════════════════════════════════════════════════════════════════════════
# 管理端 API
# ═══════════════════════════════════════════════════════════════════════════

@sub_bp.route('/admin/items', methods=['GET'])
@_admin_required
def admin_list_items():
    """管理员：列出所有 SKU"""
    svc = get_subscription_service()
    items = svc.list_items(locale='zh-CN', active_only=False)
    return jsonify({'items': items})


@sub_bp.route('/admin/items', methods=['POST'])
@_admin_required
def admin_save_item():
    """管理员：新增/更新 SKU"""
    data = request.get_json(silent=True) or {}
    if not data.get('item_key'):
        return jsonify({'error': _t('item_key required')}), 400

    svc = get_subscription_service()
    svc.upsert_item(data)
    return jsonify({'message': 'ok'})


@sub_bp.route('/admin/items/<item_key>', methods=['DELETE'])
@_admin_required
def admin_delete_item(item_key):
    """管理员：停用 SKU"""
    svc = get_subscription_service()
    item = svc.get_item(item_key)
    if not item:
        return jsonify({'error': _t('Not found')}), 404
    svc.upsert_item({
        'item_key': item_key,
        'category': item.category,
        'name_zh': item.name_zh,
        'name_en': item.name_en,
        'description_zh': item.description_zh,
        'description_en': item.description_en,
        'price_month': item.price_month,
        'price_year': item.price_year,
        'is_active': 0,
        'auto_activate': item.auto_activate,
        'sort_order': item.sort_order,
    })
    return jsonify({'message': 'ok'})


@sub_bp.route('/admin/users', methods=['GET'])
@_admin_required
def admin_list_users():
    """管理员：查看用户订阅列表（可选按用户过滤）"""
    user_id = request.args.get('user_id', type=int)
    svc = get_subscription_service()
    if user_id:
        subs = svc.get_user_subscriptions(user_id)
    else:
        subs = svc.list_all_subscriptions()
    return jsonify({'subscriptions': [s.to_dict() for s in subs]})


@sub_bp.route('/admin/orders', methods=['GET'])
@_admin_required
def admin_list_orders():
    """管理员：订单列表"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    svc = get_subscription_service()
    orders = svc.list_all_orders(limit, offset)
    return jsonify({'orders': [o.to_dict() for o in orders]})


@sub_bp.route('/admin/orders/<order_no>/refund', methods=['POST'])
@_admin_required
def admin_refund_order(order_no):
    """管理员：退款订单"""
    svc = get_subscription_service()
    success, msg = svc.refund_order(order_no)
    if success:
        return jsonify({'success': True, 'message': 'Refunded'})
    return jsonify({'success': False, 'error': msg}), 400


@sub_bp.route('/admin/stats', methods=['GET'])
@_admin_required
def admin_stats():
    """管理员：收入统计（今日/本月/MRR/趋势）"""
    svc = get_subscription_service()
    return jsonify({'stats': svc.get_stats()})


@sub_bp.route('/admin/billing', methods=['GET'])
@_admin_required
def admin_billing():
    """管理员：计费日志（sub_payment_events）"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    svc = get_subscription_service()
    return jsonify({'events': svc.list_billing_events(limit, offset)})


@sub_bp.route('/admin/deploy-codes', methods=['GET'])
@_admin_required
def admin_list_deploy_codes():
    """管理员：部署码列表"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    svc = get_subscription_service()
    return jsonify({'codes': svc.list_deploy_codes(limit, offset)})


@sub_bp.route('/admin/deploy-codes', methods=['POST'])
@_admin_required
def admin_create_deploy_code():
    """管理员：生成部署码"""
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400
    svc = get_subscription_service()
    code = svc.create_deploy_code(
        user_id=user_id,
        item_key=data.get('item_key', 'deploy_basic'),
        duration_days=data.get('duration_days', 365),
    )
    return jsonify({'code': code})


@sub_bp.route('/admin/deploy-codes/<code>', methods=['POST'])
@_admin_required
def admin_update_deploy_code(code):
    """管理员：更新部署码状态"""
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    svc = get_subscription_service()
    ok, msg = svc.update_deploy_code_status(code, status)
    if ok:
        return jsonify({'message': 'ok'})
    return jsonify({'error': msg}), 400


@sub_bp.route('/admin/', methods=['GET'])
@_admin_required
def admin_panel():
    """管理后台订阅面板"""
    return render_template('subscribe_admin.html')


# ═══════════════════════════════════════════════════════════════════════════
# 门户页面
# ═══════════════════════════════════════════════════════════════════════════

@sub_bp.route('/portal', methods=['GET'])
def subscribe_portal():
    """用户订阅管理门户"""
    return render_template('subscribe.html', translations={
        'sub.free': _t('Free'),
        'sub.per_year': _t('per_year'),
        'sub.per_month': _t('per_month'),
        'sub.monthly': _t('Monthly'),
        'sub.quarterly': _t('Quarterly'),
        'sub.semi_annual': _t('Semi-Annual'),
        'sub.yearly': _t('Yearly'),
        'sub.times_per_day': _t('times_per_day'),
        'sub.choose': _t('Choose'),
        'sub.free_register': _t('Free Sign Up'),
        'sub.confirm_pay': _t('Confirm Payment'),
        'sub.creating': _t('Creating order...'),
        'sub.login_required': _t('Please log in first'),
        'sub.redirecting_login': _t('Redirecting to login page...'),
        'sub.order_failed': _t('Failed to create order'),
        'sub.redirecting_pay': _t('Redirecting to payment...'),
        'sub.return_after_pay': _t('Return here after payment'),
        'sub.dev_no_configured': _t('Payment channel not configured, please contact admin'),
        'sub.success': _t('Subscription successful!'),
        'sub.plan_activated': _t('Plan activated'),
        'sub.confirm_failed': _t('Confirmation failed'),
        'sub.retry': _t('Retry'),
        'sub.network_error': _t('Network error, please retry'),
        'sub.console': _t('Console'),
        'sub.yearly_save': _t('Yearly only'),
        'sub.quarterly_save': _t('Quarterly only'),
        'sub.semi_annual_save': _t('Semi-Annual only'),
        'sub.save': _t('Save'),
    })
