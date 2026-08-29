#!/usr/bin/env python3
"""
Currency Converter Plugin Routes — 币种管理 API + 前台换算接口
===============================================================
"""
import os
import sys

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify, render_template

currency_bp = Blueprint('currency', __name__, url_prefix='/admin/currency',
                        template_folder='templates',
                        static_folder='static')


def _require_admin():
    """复用主系统的管理员鉴权"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    """复用主系统的操作日志"""
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


# ── 管理页面 ──────────────────────────────────────

@currency_bp.route('/', methods=['GET'])
def admin_page():
    admin, err = _require_admin()
    if err:
        return err
    return render_template('admin_currency.html')


# ── 公有 API（无需管理员登录） ──────────────────────────


@currency_bp.route('/rates', methods=['GET'])
def public_get_rates():
    """获取所有汇率映射（前端价格换算使用）"""
    from .services import get_all_rates, get_enabled_currencies, _BASE_CURRENCY
    try:
        rates = get_all_rates()
        currencies = get_enabled_currencies()
        return jsonify({
            'success': True,
            'data': {
                'base_currency': _BASE_CURRENCY,
                'rates': rates,
                'currencies': currencies,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@currency_bp.route('/convert', methods=['POST'])
def public_convert():
    """换算金额（前端防抖调用）"""
    from .services import convert, format_amount
    data = request.get_json(force=True) or {}
    amount = data.get('amount', 0)
    from_currency = data.get('from', 'CNY')
    to_currency = data.get('to', 'CNY')
    # 修复 FX-D2: 负数/零金额校验
    try:
        amount_f = float(amount)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'amount must be a number'}), 400
    if amount_f <= 0:
        return jsonify({'success': False, 'error': 'amount must be positive'}), 400
    try:
        converted, rate = convert(amount_f, from_currency, to_currency)
        return jsonify({
            'success': True,
            'data': {
                'original': float(amount),
                'from': from_currency.upper(),
                'to': to_currency.upper(),
                'converted': round(converted, 2),
                'rate': round(rate, 6),
                'formatted': format_amount(converted, to_currency),
            }
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── 用户偏好 ────────────────────────────────────────────


@currency_bp.route('/preference', methods=['GET'])
def get_preference():
    """获取当前用户币种偏好"""
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    payload = validate_token(token) if token else None
    user_id = payload.get('user_id') if payload else None
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    from .services import get_user_preferred_currency
    currency = get_user_preferred_currency(user_id)
    return jsonify({'success': True, 'data': {'currency': currency}})


@currency_bp.route('/preference', methods=['POST'])
def set_preference():
    """设置当前用户币种偏好"""
    from services.jwt_service import validate_token
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    payload = validate_token(token) if token else None
    user_id = payload.get('user_id') if payload else None
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    data = request.get_json(force=True) or {}
    currency = data.get('currency', '').strip().upper()
    if not currency:
        return jsonify({'success': False, 'error': 'Currency required'}), 400
    from .services import set_user_preferred_currency
    ok = set_user_preferred_currency(user_id, currency)
    if not ok:
        return jsonify({'success': False, 'error': 'Failed to save preference'}), 500
    return jsonify({'success': True, 'data': {'currency': currency}})


# ── GeoIP 自动检测 ──────────────────────────────────────


@currency_bp.route('/geoip', methods=['GET'])
def geoip_detect():
    """根据访客 IP 自动检测推荐币种（无需登录）"""
    from .services import detect_currency_by_ip
    # 审计 M1：取 nginx 强制覆盖的 X-Real-IP，不再信任可伪造的 X-Forwarded-For 首段
    ip = request.headers.get('X-Real-IP', request.remote_addr or '')
    result = detect_currency_by_ip(ip)
    return jsonify({'success': True, 'data': result})


# ── 管理 API（需管理员） ────────────────────────────────


@currency_bp.route('/manage/sync', methods=['POST'])
def admin_sync_rates():
    """管理员手动触发汇率同步"""
    admin, err = _require_admin()
    if err:
        return err
    from .services import sync_rates
    import asyncio
    try:
        count = asyncio.run(sync_rates())
        _log(admin['user_id'], 'sync_rates', 'currency', '', f'Synced {count} rates')
        return jsonify({'success': True, 'data': {'count': count}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@currency_bp.route('/manage/stats', methods=['GET'])
def admin_rate_stats():
    """管理员查看汇率统计（按启用币种过滤，避免展示全部缓存币种）"""
    admin, err = _require_admin()
    if err:
        return err
    from .services import _BASE_CURRENCY
    from .models import get_db
    # 启用币种：以插件配置为准（兼容 list 与逗号分隔字符串）
    pm = _get_cc_pm()
    raw = None
    if pm:
        raw = (pm.get_config('currency_converter') or {}).get('enabled_currencies')
    if raw is None:
        raw = _CC_DEFAULTS.get('enabled_currencies')
    if isinstance(raw, str):
        codes = [c.strip().upper() for c in raw.split(',') if c.strip()]
    else:
        codes = [str(c).strip().upper() for c in (raw or []) if str(c).strip()]
    try:
        with get_db() as conn:
            if codes:
                rows = conn.execute(
                    'SELECT currency_code, rate_to_base, fetched_at FROM exchange_rates'
                    ' WHERE currency_code = ANY(%s) ORDER BY fetched_at DESC',
                    (codes,)
                ).fetchall()
                rows = [dict(r) for r in rows]
            else:
                rows = []
        return jsonify({
            'success': True,
            'data': {
                'base_currency': _BASE_CURRENCY,
                'total_rates': len(rows),
                'latest_rates': rows[:10],
                'enabled_currencies': len(codes),
                'memory_cache_size': 0,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@currency_bp.route('/manage/config', methods=['GET'])
def admin_get_config():
    """获取当前配置"""
    admin, err = _require_admin()
    if err:
        return err
    from .services import _BASE_CURRENCY, _CACHE_TTL
    return jsonify({
        'success': True,
        'data': {
            'base_currency': _BASE_CURRENCY,
            'cache_ttl_seconds': _CACHE_TTL,
        }
    })


# ─── PluginManager 标准化配置 ─────────────────────────────────────────

_CC_CONFIG_KEYS = [
    'primary_api', 'fallback_api', 'base_currency', 'refresh_interval_minutes',
    'cache_ttl_minutes', 'default_currency', 'enable_geoip', 'enabled_currencies',
]

_CC_DEFAULTS = {
    'primary_api': 'https://api.frankfurter.app/latest',
    'fallback_api': 'https://open.er-api.com/v6/latest',
    'base_currency': 'CNY',
    'refresh_interval_minutes': 60,
    'cache_ttl_minutes': 60,
    'default_currency': 'CNY',
    'enable_geoip': True,
    'enabled_currencies': 'CNY,USD,EUR,JPY,GBP,HKD,KRW,AUD,CAD,SGD,THB,MYR,PHP,IDR,VND',
}


def _get_cc_pm():
    import flask
    try:
        return flask.current_app.extensions.get('plugin_manager')
    except Exception:
        return None


@currency_bp.route('/settings', methods=['GET'])
def cc_settings_get():
    admin, err = _require_admin()
    if err:
        return err
    pm = _get_cc_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    cfg = pm.get_config('currency_converter') or {}
    result = {}
    for k in _CC_CONFIG_KEYS:
        v = cfg.get(k)
        if v is not None:
            result[k] = v
        else:
            result[k] = _CC_DEFAULTS.get(k)
    return jsonify({'success': True, 'data': result})


@currency_bp.route('/settings', methods=['POST'])
def cc_settings_save():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    pm = _get_cc_pm()
    if not pm:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    filtered = {}
    for k in _CC_CONFIG_KEYS:
        if k in data:
            v = data[k]
            if k in ('refresh_interval_minutes', 'cache_ttl_minutes'):
                try:
                    filtered[k] = int(v)
                except (ValueError, TypeError):
                    return jsonify({'success': False, 'error': f'{k} must be integer'}), 400
            elif k == 'enable_geoip':
                if isinstance(v, str):
                    filtered[k] = v.lower() in ('1', 'true', 'yes')
                else:
                    filtered[k] = bool(v)
            else:
                filtered[k] = str(v) if v is not None else ''
    if not filtered:
        return jsonify({'success': False, 'error': 'No valid config keys'}), 400
    result = pm.set_config_batch('currency_converter', filtered, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': str(result['errors'])})
    return jsonify({'success': True})
