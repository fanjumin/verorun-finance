#!/usr/bin/env python3
"""
Logistics Plugin Routes — 物流查询管理 API
===========================================
完全独立，使用插件 PG schema `logistics` + 主库 system_config 只读。
"""
from i18n import _
import sys
import os

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify

from .models import get_logistics_db
from .services import query_track

logistics_bp = Blueprint('logistics', __name__, url_prefix='/admin/logistics')


def _require_admin():
    from routes.admin import _require_admin as _ra
    return _ra()


# ── POST /admin/logistics/query ──
@logistics_bp.route('/query', methods=['POST'])
def logistics_do_query():
    """查询物流轨迹"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    shipper_code = data.get('shipper_code', '').strip()
    logistic_code = data.get('logistic_code', '').strip()
    order_code = data.get('order_code', '').strip()
    customer_name = data.get('customer_name', '').strip()
    if not shipper_code or not logistic_code:
        return jsonify({'success': False, 'error': _('Logistics Company Code and Tracking Number cannot be empty')}), 400
    success, result, error = query_track(shipper_code, logistic_code, order_code, customer_name)
    if success:
        return jsonify({'success': True, 'data': result})
    return jsonify({'success': False, 'error': error}), 400


# ── GET /admin/logistics/history ──
@logistics_bp.route('/history', methods=['GET'])
def logistics_history():
    """获取查询历史"""
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    offset = (page - 1) * per_page
    with get_logistics_db() as conn:
        total = conn.execute('SELECT COUNT(*) as c FROM logistics_queries').fetchone()['c']
        rows = conn.execute(
            'SELECT * FROM logistics_queries ORDER BY queried_at DESC LIMIT %s OFFSET %s',
            (per_page, offset)
        ).fetchall()
    return jsonify({
        'success': True,
        'data': {
            'items': [dict(r) for r in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
        }
    })
