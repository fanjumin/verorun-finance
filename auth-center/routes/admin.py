#!/usr/bin/env python3
"""Admin Routes -- site management panel"""
import sys, os, json, socket, time
from datetime import datetime, timedelta

# ═══ Cache stdlib platform BEFORE inserting project root into sys.path ═══
# Prevents project's platform/ directory from shadowing stdlib platform module
import platform as _stdlib_platform
_ = _stdlib_platform.system

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flask import Blueprint, request, jsonify
from i18n import _
from models import get_db
from plugin_manager.hooks import get_hook_registry
from services.dashboard_service import DashboardService

# Create a single instance for the application
_dashboard_svc = DashboardService()

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# Dashboard 内存缓存（请求合并，5s TTL 让 per-widget 缓存主导刷新策略）
_dash_cache = {'data': None, 'ts': 0, 'ttl': 5}
# 通用 GET 请求内存缓存（5 秒 TTL），消除重复点击同一模块的等待感
_get_cache = {}

def _cached_get(ttl=5):
    """装饰器：对 GET 请求做内存缓存"""
    from functools import wraps
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if request.method != 'GET':
                return fn(*a, **kw)
            key = request.path + '?' + request.query_string.decode()
            now = time.time()
            entry = _get_cache.get(key)
            if entry and (now - entry['ts']) < ttl:
                from flask import make_response
                return make_response((entry['body'], entry['status']))
            resp = fn(*a, **kw)
            # 提取状态码和 body，重建 Response 缓存
            if hasattr(resp, 'status_code') and resp.status_code == 200:
                _get_cache[key] = {'body': resp.get_data(as_text=True), 'status': 200, 'ts': now}
            elif isinstance(resp, tuple) and len(resp) == 2 and getattr(resp[0], 'status_code', None) == 200:
                _get_cache[key] = {'body': resp[0].get_data(as_text=True), 'status': 200, 'ts': now}
            return resp
        return wrapper
    return deco

def _require_admin():
    """鉴权守卫 — 与 agent_matrix/site_builder 版本对齐：
    1. 优先从 Authorization header 提取 token
    2. 无 header 时回退到 sso_token / tm_token cookie
    3. 使用 JWT is_admin 声明，不再冗余查询数据库
    """
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return None, (jsonify({'success': False, 'error': _('Requires management permissions')}), 401)
    return {'user_id': payload['user_id'], 'nickname': ''}, None


def _log(admin_id, action, target_type="", target_id="", detail=""):
    ip = request.remote_addr or ''
    with get_db() as conn:
        conn.execute(
            'INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail, ip_address) VALUES (%s,%s,%s,%s,%s,%s)',
            (admin_id, action, target_type, target_id, detail, ip)
        )
        conn.commit()


@admin_bp.route('/logout', methods=['POST'])
def admin_logout():
    """管理员退出登录"""
    admin, err = _require_admin()
    if err:
        return err
    _log(admin['user_id'], 'logout', 'admin', '', chr(39)+chr(39))
    return jsonify({'success': True})


@admin_bp.route('/dashboard', methods=['GET'])
def dashboard():
    """Full dashboard data - uses DashboardService for modular querying."""
    admin, err = _require_admin()
    if err:
        return err

    now = time.time()
    if _dash_cache['data'] is not None and (now - _dash_cache['ts']) < _dash_cache['ttl']:
        return jsonify({"success": True, "data": _dash_cache['data']})

    try:
        # Use the new DashboardService for data retrieval
        data = _dashboard_svc.get_full_dashboard_data()
        
        if not any(k in data for k in ('total_users','total_agents','active_subscriptions')):
            raise Exception('Core metrics all failed')
            
        _dash_cache['data'] = data
        _dash_cache['ts'] = time.time()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route('/dashboard/widget/<name>', methods=['GET'])
def dashboard_widget(name):
    """Get data for a specific dashboard widget."""
    admin, err = _require_admin()
    if err:
        return err

    try:
        result = _dashboard_svc.get_widget_data(name)
        if result.get('success'):
            return jsonify(result)
        else:
            return jsonify(result), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route('/dashboard/realtime', methods=['GET'])
def dashboard_realtime():
    """Get real-time metrics (online users, PV, UV, health)."""
    admin, err = _require_admin()
    if err:
        return err

    try:
        data = _dashboard_svc.get_realtime_data()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@admin_bp.route('/dashboard/summary', methods=['GET'])
def dashboard_summary():
    """Get lightweight summary for admin header bar."""
    admin, err = _require_admin()
    if err:
        return err

    try:
        data = _dashboard_svc.get_summary_data()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ════════════════════════════════════════════════════════════════
# 收入看板
# ════════════════════════════════════════════════════════════════

@admin_bp.route('/revenue/dashboard', methods=['GET'])
def revenue_dashboard():
    """收入看板 — 综合收入统计"""
    admin, err = _require_admin()
    if err:
        return err

    try:
        return _revenue_dashboard_data()
    except Exception as e:
        import traceback
        traceback.print_exc()
        current_app.logger.error(f"Revenue dashboard failed: {traceback.format_exc()}")
        return jsonify({"success": False, "error": f"Revenue dashboard unavailable: {e}"}), 500


def _revenue_dashboard_data():
    with get_db() as conn:
        # ── Python date precomputation ──
        now = datetime.utcnow()
        today_str = now.strftime('%Y-%m-%d')
        this_month_start = now.replace(day=1).strftime('%Y-%m-%d')
        next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        next_month_start = next_month.strftime('%Y-%m-%d')
        last_month_end = now.replace(day=1).strftime('%Y-%m-%d')
        last_month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime('%Y-%m-%d')
        this_year_start = now.replace(month=1, day=1).strftime('%Y-%m-%d')
        next_year_start = now.replace(month=1, day=1, year=now.year + 1).strftime('%Y-%m-%d')
        twelve_months_ago = (now - timedelta(days=365)).strftime('%Y-%m-%d')
        thirty_days_ago = (now - timedelta(days=30)).strftime('%Y-%m-%d')
        # ── 收入汇总 ──
        today = float(conn.execute("""
            SELECT COALESCE(SUM(amount),0) as rev FROM billing_orders
            WHERE status='paid' AND date(paid_at)=%s
        """, (today_str,)).fetchone()['rev'] or 0)
        today += float(conn.execute("""
            SELECT COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' AND date(paid_at)=%s
        """, (today_str,)).fetchone()['rev'] or 0)
        today += float(conn.execute("""
            SELECT COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid' AND date(paid_at)=%s
        """, (today_str,)).fetchone()['rev'] or 0)

        this_month = float(conn.execute("""
            SELECT COALESCE(SUM(amount),0) as rev FROM billing_orders
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (this_month_start, next_month_start)).fetchone()['rev'] or 0)
        this_month += float(conn.execute("""
            SELECT COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (this_month_start, next_month_start)).fetchone()['rev'] or 0)
        this_month += float(conn.execute("""
            SELECT COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (this_month_start, next_month_start)).fetchone()['rev'] or 0)

        this_year = float(conn.execute("""
            SELECT COALESCE(SUM(amount),0) as rev FROM billing_orders
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (this_year_start, next_year_start)).fetchone()['rev'] or 0)
        this_year += float(conn.execute("""
            SELECT COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (this_year_start, next_year_start)).fetchone()['rev'] or 0)
        this_year += float(conn.execute("""
            SELECT COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (this_year_start, next_year_start)).fetchone()['rev'] or 0)

        # ── 上月收入（环比） ──
        last_month = float(conn.execute("""
            SELECT COALESCE(SUM(amount),0) as rev FROM billing_orders
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (last_month_start, last_month_end)).fetchone()['rev'] or 0)
        last_month += float(conn.execute("""
            SELECT COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (last_month_start, last_month_end)).fetchone()['rev'] or 0)
        last_month += float(conn.execute("""
            SELECT COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid' AND paid_at>=%s AND paid_at<%s
        """, (last_month_start, last_month_end)).fetchone()['rev'] or 0)

        # ── 近30天每日收入趋势 ──
        trend = conn.execute("""
            SELECT date(paid_at) as day, SUM(amount) as rev FROM billing_orders
            WHERE status='paid' AND paid_at>=%s
            GROUP BY date(paid_at) ORDER BY day
        """, (thirty_days_ago,)).fetchall()
        trend_map = {r['day']: float(r['rev']) for r in trend}
        # Add subscription orders
        sub_trend = conn.execute("""
            SELECT date(paid_at) as day, COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' AND paid_at>=%s
            GROUP BY date(paid_at) ORDER BY day
        """, (thirty_days_ago,)).fetchall()
        for r in sub_trend:
            trend_map[r['day']] = trend_map.get(r['day'], 0.0) + float(r['rev'])
        # Add shop orders
        shop_trend = conn.execute("""
            SELECT date(paid_at) as day, COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid' AND paid_at>=%s
            GROUP BY date(paid_at) ORDER BY day
        """, (thirty_days_ago,)).fetchall()
        for r in shop_trend:
            trend_map[r['day']] = trend_map.get(r['day'], 0.0) + float(r['rev'])

        # ── 近12月月度收入 ──
        monthly = conn.execute("""
            SELECT to_char(paid_at, 'YYYY-MM') as ym, SUM(amount) as rev FROM billing_orders
            WHERE status='paid' AND paid_at>=%s
            GROUP BY ym ORDER BY ym
        """, (twelve_months_ago,)).fetchall()
        monthly_map = {r['ym']: float(r['rev']) for r in monthly}
        sub_monthly = conn.execute("""
            SELECT to_char(paid_at, 'YYYY-MM') as ym, COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' AND paid_at>=%s
            GROUP BY ym ORDER BY ym
        """, (twelve_months_ago,)).fetchall()
        for r in sub_monthly:
            monthly_map[r['ym']] = monthly_map.get(r['ym'], 0.0) + float(r['rev'])
        shop_monthly = conn.execute("""
            SELECT to_char(paid_at, 'YYYY-MM') as ym, COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid' AND paid_at>=%s
            GROUP BY ym ORDER BY ym
        """, (twelve_months_ago,)).fetchall()
        for r in shop_monthly:
            monthly_map[r['ym']] = monthly_map.get(r['ym'], 0.0) + float(r['rev'])

        # ── 收入按类型分类 ──
        by_type = {}
        raw = conn.execute("""
            SELECT item_type, COALESCE(SUM(amount),0) as rev FROM billing_orders
            WHERE status='paid' GROUP BY item_type
        """).fetchall()
        for r in raw:
            by_type[r['item_type']] = by_type.get(r['item_type'], 0) + r['rev']
        sub_raw = conn.execute("""
            SELECT item_type, COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders
            WHERE status='paid' GROUP BY item_type
        """).fetchall()
        for r in sub_raw:
            by_type[r['item_type']] = by_type.get(r['item_type'], 0) + r['rev']
        shop_raw = conn.execute("""
            SELECT 'shop' as item_type, COALESCE(SUM(subtotal),0) as rev FROM order_items
            WHERE status='paid'
        """).fetchall()
        for r in shop_raw:
            if r['rev'] > 0:
                by_type['shop'] = by_type.get('shop', 0) + r['rev']

        # ── 支付方式分布 ──
        pay_methods = {}
        pm = conn.execute("""
            SELECT payment_method, COALESCE(SUM(amount),0) as rev FROM billing_orders
            WHERE status='paid' AND payment_method!='' GROUP BY payment_method
        """).fetchall()
        for r in pm:
            pay_methods[r['payment_method']] = pay_methods.get(r['payment_method'], 0) + r['rev']

        # ── 订阅数据 (MRR) ──
        mrr = conn.execute("""
            SELECT COALESCE(SUM(
                CASE WHEN s.interval_type='year' THEN i.price_year/12 ELSE i.price_month END
            ),0) as mrr FROM subscription.user_subscriptions s
            JOIN subscription.sub_items i ON i.item_key=s.item_key
            WHERE s.status='active'
        """).fetchone()['mrr']
        active_subs = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions WHERE status='active'
        """).fetchone()['c']

        # ── 总付费用户数 ──
        total_paid_users = conn.execute("""
            SELECT COUNT(DISTINCT user_id) as c FROM subscription.user_subscriptions WHERE status='active'
        """).fetchone()['c']

        # ── 总交易额 ──
        total_revenue = conn.execute("""
            SELECT COALESCE(SUM(amount),0) as rev FROM billing_orders WHERE status='paid'
        """).fetchone()['rev']
        total_revenue += (conn.execute("""
            SELECT COALESCE(SUM(amount_fen)/100.0,0) as rev FROM subscription.sub_orders WHERE status='paid'
        """).fetchone()['rev'] or 0)
        total_revenue += (conn.execute("""
            SELECT COALESCE(SUM(subtotal),0) as rev FROM order_items WHERE status='paid'
        """).fetchone()['rev'] or 0)

        # ── 待处理退款 ──
        pending_refunds = conn.execute("""
            SELECT COUNT(*) as c FROM billing_orders
            WHERE status='refund_pending'
        """).fetchone()['c']

        # ── 流失率计算 ──
        # 本月流失率 = 本月取消数 / 月初活跃数
        # 本月取消数
        canceled = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions
            WHERE status='canceled'
              AND canceled_at>=%s AND canceled_at<%s
        """, (this_month_start, next_month_start)).fetchone()
        active_start_month = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions
            WHERE status='active'
              AND (canceled_at IS NULL OR canceled_at >= %s)
              AND created_at < %s
        """, (this_month_start, this_month_start)).fetchone()['c'] or 1
        churn_rate = round((canceled['c'] / active_start_month) * 100, 2) if active_start_month > 0 else 0

        # 上月流失率
        last_month_canceled = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions
            WHERE status='canceled'
              AND canceled_at>=%s AND canceled_at<%s
        """, (last_month_start, last_month_end)).fetchone()['c']
        last_month_active_start = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions
            WHERE status='active'
              AND canceled_at >= %s
              AND created_at < %s
        """, (last_month_start, last_month_start)).fetchone()['c'] or 1
        last_churn_rate = round((last_month_canceled / last_month_active_start) * 100, 2) if last_month_active_start > 0 else 0

        # ── 近12月月度流失率趋势 ──
        churn_trend = []
        for i in range(11, -1, -1):
            ref_date = now.replace(day=1) - timedelta(days=30*i)
            ref_date = ref_date.replace(day=1)
            ms = ref_date.strftime('%Y-%m-%d')
            me = (ref_date.replace(day=1) + timedelta(days=32)).replace(day=1).strftime('%Y-%m-%d')
            m_canceled = conn.execute("""
                SELECT COUNT(*) as c FROM subscription.user_subscriptions
                WHERE status='canceled'
                  AND canceled_at >= %s AND canceled_at < %s
            """, (ms, me)).fetchone()['c']
            m_active_start = conn.execute("""
                SELECT COUNT(*) as c FROM subscription.user_subscriptions
                WHERE status='active'
                  AND (canceled_at IS NULL OR canceled_at >= %s)
                  AND created_at < %s
            """, (ms, ms)).fetchone()['c'] or 1
            m_churn = round((m_canceled / m_active_start) * 100, 2)
            ym_label = ref_date.strftime('%Y-%m')
            churn_trend.append({'ym': ym_label, 'churn_rate': m_churn, 'canceled': m_canceled, 'active_start': m_active_start})

        # ── 近30天活跃订阅趋势 ──
        sub_trend_30d = []
        for i in range(29, -1, -1):
            day = (datetime.now() - timedelta(days=i)).date().isoformat()
            active_count = conn.execute(f"""
                SELECT COUNT(*) as c FROM subscription.user_subscriptions
                WHERE status='active'
                  AND date(created_at) <= %s
                  AND (canceled_at IS NULL OR date(canceled_at) > %s)
            """, (day, day)).fetchone()['c']
            sub_trend_30d.append({'day': day, 'active_count': active_count})

        # 本月新增订阅（含 trialing 和 past_due 中本月创建的）
        new_this_month = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions
            WHERE created_at>=%s AND created_at<%s
        """, (this_month_start, next_month_start)).fetchone()['c'] + conn.execute("""
            SELECT COUNT(*) as c FROM subscription.sub_orders
            WHERE created_at>=%s AND created_at<%s AND status='paid'
        """, (this_month_start, next_month_start)).fetchone()['c']

        # 本月已过期
        expired_this_month = conn.execute("""
            SELECT COUNT(*) as c FROM subscription.user_subscriptions
            WHERE status='expired'
              AND updated_at>=%s AND updated_at<%s
        """, (this_month_start, next_month_start)).fetchone()['c']

    return jsonify({"success": True, "data": {
        'summary': {
            'today_revenue': round(today, 2),
            'this_month': round(this_month, 2),
            'last_month': round(last_month, 2),
            'this_year': round(this_year, 2),
            'total_revenue': round(total_revenue, 2),
            'month_change': round(this_month - last_month, 2),
            'month_change_pct': round(((this_month - last_month) / last_month * 100) if last_month > 0 else 0, 1),
        },
        'subscriptions': {
            'mrr': round(float(mrr) / 100, 2),
            'active': active_subs,
            'total_paid_users': total_paid_users,
            'new_this_month': new_this_month,
            'canceled_this_month': canceled['c'],
            'expired_this_month': expired_this_month,
            'churn_rate': churn_rate,
            'last_churn_rate': last_churn_rate,
            'churn_trend_12m': churn_trend,
            'active_trend_30d': sub_trend_30d,
        },
        'pending_refunds': pending_refunds,
        'trend_30d': [{'day': k, 'revenue': round(v, 2)} for k, v in sorted(trend_map.items())],
        'monthly_12m': [{'ym': k, 'revenue': round(v, 2)} for k, v in sorted(monthly_map.items())],
        'by_type': [{'type': k, 'revenue': round(v, 2)} for k, v in sorted(by_type.items(), key=lambda x: -x[1])],
        'pay_methods': [{'method': k, 'revenue': round(v, 2)} for k, v in sorted(pay_methods.items(), key=lambda x: -x[1])],
    }})



# Import split route modules to register all routes on admin_bp
from . import admin_users, admin_content, admin_system  # noqa: E402,F401
