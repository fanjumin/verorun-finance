#!/usr/bin/env python3
"""
Coupon Plugin — Routes
=======================
管理 API + 前台 API。
"""

from i18n import _
import json
import secrets
from datetime import datetime
from flask import Blueprint, request, jsonify
from functools import wraps

from plugins.coupons.engine import CouponEngine
from plugins.coupons.ai_recommender import AICouponRecommender
from plugins.coupons.scene import SceneName, get_scene_label

coupon_bp = Blueprint('coupons', __name__)

# 在 init_routes 时注入
_engine: CouponEngine = None
_recommender: AICouponRecommender = None
_get_db = None
_get_main_db = None
_t = None


def init_routes(get_db, get_main_db, engine: CouponEngine, recommender: AICouponRecommender, t_func=None):
    global _get_db, _get_main_db, _engine, _recommender, _t
    _get_db = get_db
    _get_main_db = get_main_db
    _engine = engine
    _recommender = recommender
    _t = t_func or (lambda s: s)


# ── Helpers ──

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _require_auth():
    """从请求头解析用户 JWT（使用主系统 validate_token）。"""
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, jsonify({'success': False, 'error': _t(_('Not logged in'))}), 401
    token = auth[7:]
    try:
        payload = validate_token(token)
        if not payload:
            return None, jsonify({'success': False, 'error': _t(_('Token is invalid or expired'))}), 401
        return payload, None, None
    except Exception:
        return None, jsonify({'success': False, 'error': _t(_('Token is invalid or expired'))}), 401


def _require_admin():
    """要求 admin 身份。"""
    payload, err_resp, status = _require_auth()
    if err_resp:
        return None, err_resp
    if not payload.get('is_admin'):
        return None, jsonify({'success': False, 'error': _t(_('No Permission'))}), 403
    return payload, None


def _check_rate_limit(uid, action, max_requests=30, window=60):
    """简易限流。"""
    try:
        with _get_main_db() as conn:
            row = conn.execute(
                '''SELECT COUNT(*) as c FROM api_logs
                   WHERE user_id=%s AND action=%s AND created_at > NOW() + %s::INTERVAL''',
                (uid, action, f'-{window} seconds')
            ).fetchone()
            return (row['c'] if row else 0) < max_requests
    except Exception:
        return True


def _log_admin_action(conn, admin_id, action, target_type, target_id, detail=''):
    try:
        conn.execute(
            '''INSERT INTO admin_actions (admin_id, action, target_type, target_id, detail, ip, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,NOW())''',
            (admin_id, action, target_type, target_id, detail, request.remote_addr or '')
        )
    except Exception:
        pass


# ═══════════════════════════════════════════
# 管理 API
# ═══════════════════════════════════════════

@coupon_bp.route('/admin/list', methods=['GET'])
def admin_list():
    payload, err = _require_admin()
    if err:
        return err, 403
    rows = _engine.list_all()
    for d in rows:
        limit = d.get('usage_limit', 0) or d.get('max_uses', 0)
        d['usage_rate'] = round(d['used_count'] / limit * 100, 1) if limit > 0 else 0
    return jsonify({'success': True, 'data': rows})


@coupon_bp.route('/admin/create', methods=['POST'])
def admin_create():
    payload, err = _require_admin()
    if err:
        return err, 403
    data = request.get_json() or {}
    if not data.get('code') or data.get('value') is None:
        return jsonify({'success': False, 'error': _t(_('Missing required field'))}), 400
    try:
        cid = _engine.create(data)
        with _get_db() as conn:
            _log_admin_action(conn, payload['user_id'], 'create', 'coupon', cid, data['code'])
        return jsonify({'success': True, 'data': {'id': cid}, 'message': _t(_('Coupon has been created'))})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@coupon_bp.route('/admin/update/<int:cid>', methods=['POST'])
def admin_update(cid):
    payload, err = _require_admin()
    if err:
        return err, 403
    data = request.get_json() or {}
    _engine.update(cid, data)
    with _get_db() as conn:
        _log_admin_action(conn, payload['user_id'], 'update', 'coupon', cid)
    return jsonify({'success': True, 'message': _t(_('Coupon has been updated'))})


@coupon_bp.route('/admin/delete/<int:cid>', methods=['POST'])
def admin_delete(cid):
    payload, err = _require_admin()
    if err:
        return err, 403
    _engine.delete(cid)
    with _get_db() as conn:
        _log_admin_action(conn, payload['user_id'], 'delete', 'coupon', cid)
    return jsonify({'success': True, 'message': _t(_('Coupon has been deleted'))})


@coupon_bp.route('/admin/stats', methods=['GET'])
def admin_stats():
    payload, err = _require_admin()
    if err:
        return err, 403
    return jsonify({'success': True, 'data': _engine.stats()})


@coupon_bp.route('/admin/distribute', methods=['POST'])
def admin_distribute():
    payload, err = _require_admin()
    if err:
        return err, 403
    data = request.get_json() or {}
    coupon_id = data.get('coupon_id')
    user_ids = data.get('user_ids', [])
    all_users = data.get('all_users', False)
    if not coupon_id:
        return jsonify({'success': False, 'error': _t('Please specify coupon ID')}), 400
    if not user_ids and not all_users:
        return jsonify({'success': False, 'error': _t('Please specify users')}), 400
    if all_users:
        with _get_main_db() as conn:
            rows = conn.execute('SELECT id FROM users WHERE active=1').fetchall()
            user_ids = [r['id'] for r in rows]
    count = _engine.distribute(coupon_id, user_ids)
    with _get_db() as conn:
        _log_admin_action(conn, payload['user_id'], 'distribute', 'coupon', coupon_id,
                          f'{_t("Sent to")}{count}{_t("users")}')
    return jsonify({'success': True, 'data': {'total': count},
                    'message': f'{_t("Sent to")} {count} {_t("users")}'})


@coupon_bp.route('/admin/redemptions/<int:cid>', methods=['GET'])
def admin_redemptions(cid):
    payload, err = _require_admin()
    if err:
        return err, 403
    page = _safe_int(request.args.get('page', 1))
    limit = _safe_int(request.args.get('limit', 50))
    return jsonify({'success': True, 'data': _engine.get_redemptions(cid, page, limit)})


# ═══════════════════════════════════════════
# 前台 API
# ═══════════════════════════════════════════

@coupon_bp.route('/validate', methods=['POST'])
def api_validate():
    """验证优惠券（供前端结算时调用）。"""
    payload, err, status = _require_auth()
    if err:
        return err, status
    uid = payload['user_id']
    if not _check_rate_limit(uid, 'coupon_validate'):
        return jsonify({'success': False, 'error': _t(_('Too frequent actions'))}), 429
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    amount = _safe_float(data.get('amount', 0))
    quantity = _safe_int(data.get('quantity', 0))
    product_id = data.get('product_id')
    scene = data.get('scene', '')
    if not code:
        return jsonify({'success': False, 'error': _t('Please enter a coupon code')}), 400
    result = _engine.validate(code, amount, user_id=uid, quantity=quantity,
                              product_id=product_id, scene=scene or None)
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
            'scene': cpn.get('scene', ''),
            'value': cpn['value'],
            'discount': result['discount'],
        }
    })


@coupon_bp.route('/user/list', methods=['GET'])
def api_user_coupons():
    """获取用户的可用优惠券列表。"""
    payload, err, status = _require_auth()
    if err:
        return err, status
    uid = payload['user_id']
    data = _engine.get_user_coupons(uid)
    return jsonify({'success': True, 'data': data})


@coupon_bp.route('/user/available', methods=['POST'])
def api_available():
    """获取用户在指定场景+金额下的可用优惠券（供 AI 推荐使用）。"""
    payload, err, status = _require_auth()
    if err:
        return err, status
    uid = payload['user_id']
    data = request.get_json() or {}
    amount = _safe_float(data.get('amount', 0))
    scene = data.get('scene', '')
    coupons = _engine.get_available_coupons(uid, amount, scene=scene or None)
    return jsonify({'success': True, 'data': coupons})


@coupon_bp.route('/ai/recommend', methods=['POST'])
def api_ai_recommend():
    """AI 智能推荐优惠券（结算页调用）。"""
    payload, err, status = _require_auth()
    if err:
        return err, status
    uid = payload['user_id']
    data = request.get_json() or {}
    amount = _safe_float(data.get('amount', 0))
    scene = data.get('scene', '')
    locale = data.get('locale', 'zh-CN')
    result = _recommender.recommend(uid, amount, scene=scene or None, locale=locale)
    # 序列化 coupon（避免前端无法处理）
    if result.get('best'):
        result['best'] = {k: v for k, v in result['best'].items()
                          if not k.startswith('_(') and isinstance(v, (str, int, float, bool, type(None)))}
    result['recommended'] = [
        {k: v for k, v in c.items()
         if not k.startswith('_(') and isinstance(v, (str, int, float, bool, type(None)))}
        for c in result.get('recommended', [])
    ]
    return jsonify({'success': True, 'data': result})


@coupon_bp.route('/scenes', methods=['GET'])
def api_scenes():
    """返回所有场景列表（供前端下拉选择）。"""
    locale = request.args.get('locale', 'zh-CN')
    scenes = []
    for name in dir(SceneName):
        if name.startswith('_'):
            continue
        val = getattr(SceneName, name)
        if isinstance(val, str) and val.startswith(('shop_', 'purchase_', 'ai_', 'subscription_', 'new_', 'promo_', 'referral_')):
            scenes.append({
                'name': val,
                'label': get_scene_label(val, locale),
            })
    return jsonify({'success': True, 'data': scenes})


@coupon_bp.route('/apply', methods=['POST'])
def api_apply():
    """将优惠券应用到订单（供结算流程调用）。"""
    payload, err, status = _require_auth()
    if err:
        return err, status
    uid = payload['user_id']
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    order_no = data.get('order_no', '')
    amount = _safe_float(data.get('amount', 0))
    if not code or not order_no:
        return jsonify({'success': False, 'error': _t(_('Missing parameter'))}), 400
    result = _engine.apply_to_order(code, uid, order_no, amount)
    if not result['success']:
        return jsonify({'success': False, 'error': result['error']}), 400
    return jsonify({'success': True, 'data': {
        'discount': result['discount'],
        'coupon_id': result['coupon_id'],
    }})


# ═══════════════════════════════════════════
# 兼容桥接 API（保持与旧系统一致）
# ═══════════════════════════════════════════

@coupon_bp.route('/bridge/list', methods=['GET'])
def bridge_list():
    """与旧 shop_admin.py:list_coupons 一致。"""
    payload, err = _require_admin()
    if err:
        return err, 403
    rows = _engine.list_all()
    for d in rows:
        limit = d.get('usage_limit', 0) or d.get('max_uses', 0)
        d['usage_rate'] = round(d['used_count'] / limit * 100, 1) if limit > 0 else 0
    return jsonify({'success': True, 'data': rows})


@coupon_bp.route('/bridge/stats', methods=['GET'])
def bridge_stats():
    return admin_stats()


@coupon_bp.route('/bridge/redemptions/<int:cid>', methods=['GET'])
def bridge_redemptions(cid):
    return admin_redemptions(cid)
