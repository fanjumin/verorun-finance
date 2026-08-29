#!/usr/bin/env python3
"""Ad Management Plugin — 广告管理 API 路由 (v1.2.0)"""
from i18n import _
import sys, os, json
import time as _time
from datetime import datetime

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify, current_app

ads_bp = Blueprint('ads', __name__, url_prefix='/admin/ads')


def _require_admin():
    """复用主系统的管理员鉴权"""
    from routes.admin import _require_admin as _ra
    return _ra()


def _log(admin_id, action, target_type='', target_id='', detail=''):
    """复用主系统的操作日志"""
    from routes.admin import _log as _l
    _l(admin_id, action, target_type, target_id, detail)


# ── 轻量内存滑动窗口限流（与 admin/app.py 既有模式一致，无外部依赖） ──
_ADS_RATE_LIMIT = {}


def _rate_limit(key, max_per_minute):
    """滑动窗口限流，返回 True 表示允许通过"""
    now = _time.time()
    window = 60.0
    stamps = _ADS_RATE_LIMIT.setdefault(key, [])
    stamps[:] = [t for t in stamps if now - t < window]
    if len(stamps) >= max_per_minute:
        return False
    stamps.append(now)
    return True


# ============================================================
# 通用辅助
# ============================================================

def _now_str():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')


def _ad_row_to_dict(row):
    r = dict(row)
    try:
        r['targeting_rules'] = json.loads(r.get('targeting_rules') or '{}')
    except (json.JSONDecodeError, TypeError):
        r['targeting_rules'] = {}
    return r


# ============================================================
# 广告位 CRUD
# ============================================================

# ── GET /admin/ads ──
@ads_bp.route('/', methods=['GET'])
@ads_bp.route('', methods=['GET'])
def list_ads():
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    offset = (page - 1) * limit
    site_key = request.args.get('site_key', '').strip() or None
    zone_id = request.args.get('zone_id', type=int)

    from plugins.ads.models import get_ads_db

    where = []
    params = []
    if site_key:
        where.append('site_key=%s')
        params.append(site_key)
    if zone_id is not None:
        where.append('zone_id=%s')
        params.append(zone_id)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with get_ads_db() as conn:
        total = conn.execute(f'SELECT COUNT(*) as c FROM ad_placements {where_sql}', params).fetchone()['c']
        rows = conn.execute(
            f'SELECT * FROM ad_placements {where_sql} ORDER BY sort_order, id LIMIT %s OFFSET %s',
            params + [limit, offset]
        ).fetchall()
    return jsonify({
        'success': True,
        'data': [_ad_row_to_dict(r) for r in rows],
        'total': total, 'page': page, 'limit': limit
    })


# ── POST /admin/ads ──
@ads_bp.route('', methods=['POST'])
def create_ad():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = str(data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': _('Advertisement name cannot be empty')}), 400

    from plugins.ads.models import create_ad_record
    try:
        ad_id = create_ad_record(data)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{_("Creation failed")}: {e}'}), 400
    _log(admin['user_id'], 'create_ad', detail=f'id={ad_id} name={name}')
    return jsonify({'success': True, 'data': {'id': ad_id}})


# ── PUT /admin/ads/<id> ──
@ads_bp.route('/<int:ad_id>', methods=['PUT'])
def update_ad(ad_id):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    from plugins.ads.models import update_ad_record, AdNotFound
    try:
        update_ad_record(ad_id, data)
    except AdNotFound:
        return jsonify({'success': False, 'error': _('Advertisement does not exist')}), 404
    except ValueError:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    _log(admin['user_id'], 'update_ad', detail=f'id={ad_id}')
    return jsonify({'success': True})


# ── DELETE /admin/ads/<id> ──
@ads_bp.route('/<int:ad_id>', methods=['DELETE'])
def delete_ad(ad_id):
    admin, err = _require_admin()
    if err:
        return err
    from plugins.ads.models import delete_ad_record
    delete_ad_record(ad_id)
    _log(admin['user_id'], 'delete_ad', detail=f'id={ad_id}')
    return jsonify({'success': True})


# ============================================================
# 广告位区域（Zone）管理
# ============================================================

@ads_bp.route('/zones', methods=['GET'])
def list_zones():
    admin, err = _require_admin()
    if err:
        return err
    site_key = request.args.get('site_key', '').strip() or 'default'
    active_only = request.args.get('active_only', '').lower() == 'true'
    from plugins.ads.models import list_zones as _list_zones
    return jsonify({'success': True, 'data': _list_zones(site_key=site_key, active_only=active_only)})


@ads_bp.route('/zones', methods=['POST'])
def create_zone():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    identifier = data.get('identifier', '').strip()
    if not name or not identifier:
        return jsonify({'success': False, 'error': _('Region name and identifier cannot be empty')}), 400
    from plugins.ads.models import create_zone as _create_zone
    try:
        zone_id = _create_zone(data)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{_("Creation failed")}: {e}'}), 400
    _log(admin['user_id'], 'create_ad_zone', detail=f'id={zone_id} name={name}')
    return jsonify({'success': True, 'data': {'id': zone_id}})


@ads_bp.route('/zones/<int:zone_id>', methods=['PUT'])
def update_zone(zone_id):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    from plugins.ads.models import update_zone as _update_zone, get_zone
    if not get_zone(zone_id):
        return jsonify({'success': False, 'error': _('Region does not exist')}), 404
    _update_zone(zone_id, data)
    _log(admin['user_id'], 'update_ad_zone', detail=f'id={zone_id}')
    return jsonify({'success': True})


@ads_bp.route('/zones/<int:zone_id>', methods=['DELETE'])
def delete_zone(zone_id):
    admin, err = _require_admin()
    if err:
        return err
    from plugins.ads.models import delete_zone as _delete_zone, get_zone, count_zone_ads
    if not get_zone(zone_id):
        return jsonify({'success': False, 'error': _('Region does not exist')}), 404
    refs = count_zone_ads(zone_id)
    if refs:
        return jsonify({'success': False,
                        'error': _('Region is referenced by {} ad(s); reassign or delete them first').format(refs)}), 400
    _delete_zone(zone_id)
    _log(admin['user_id'], 'delete_ad_zone', detail=f'id={zone_id}')
    return jsonify({'success': True})


# ============================================================
# 统计上报与查询
# ============================================================

@ads_bp.route('/api/v1/stats/impression', methods=['POST'])
def api_record_impression():
    """公开端点：上报展示（IP 维度限流，防刷量）"""
    client = request.remote_addr or 'unknown'
    if not _rate_limit(f'impression:{client}', 60):
        return jsonify({'success': False, 'error': _('Too many requests')}), 429
    data = request.get_json() or {}
    ad_id = data.get('ad_id')
    if not ad_id:
        return jsonify({'success': False, 'error': _('ad_id required')}), 400
    try:
        from plugins.ads.models import record_impression
        record_impression(ad_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ads_bp.route('/api/v1/stats/click', methods=['POST'])
def api_record_click():
    """公开端点：上报点击（IP 维度限流，防刷量）"""
    client = request.remote_addr or 'unknown'
    if not _rate_limit(f'click:{client}', 30):
        return jsonify({'success': False, 'error': _('Too many requests')}), 429
    data = request.get_json() or {}
    ad_id = data.get('ad_id')
    if not ad_id:
        return jsonify({'success': False, 'error': _('ad_id required')}), 400
    try:
        from plugins.ads.models import record_click
        record_click(
            ad_id,
            page=data.get('page', ''),
            position=data.get('position', ''),
            ip=request.remote_addr or '',
            user_agent=request.headers.get('User-Agent', ''),
            referrer=request.headers.get('Referer', '')
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@ads_bp.route('/api/v1/stats', methods=['GET'])
def api_get_stats():
    """管理端点：查询统计（需管理员权限）"""
    admin, err = _require_admin()
    if err:
        return err
    ad_id = request.args.get('ad_id', type=int)
    site_key = request.args.get('site_key', '').strip() or None
    days = request.args.get('days', 7, type=int)
    days = max(1, min(days, 90))
    from plugins.ads.models import get_ad_stats
    return jsonify({'success': True, 'data': get_ad_stats(ad_id=ad_id, site_key=site_key, days=days)})


# ============================================================
# 插件设置
# ============================================================

_ADS_CONFIG_KEYS = ['default_width', 'default_height', 'max_placements']


@ads_bp.route('/settings', methods=['GET'])
def ads_settings_get():
    """获取插件配置"""
    admin, err = _require_admin()
    if err:
        return err
    from flask import current_app
    mgr = current_app.extensions.get('plugin_manager')
    cfg = mgr.get_config('ads') if mgr else {}
    return jsonify({'success': True, 'data': {
        'config': {k: cfg.get(k, '') for k in _ADS_CONFIG_KEYS},
    }})


@ads_bp.route('/settings', methods=['POST'])
def ads_settings_save():
    """保存插件配置"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    from flask import current_app
    mgr = current_app.extensions.get('plugin_manager')
    if not mgr:
        return jsonify({'success': False, 'error': 'PluginManager not available'}), 503
    cfg = {}
    for k in _ADS_CONFIG_KEYS:
        if k in data:
            cfg[k] = data[k]
    if not cfg:
        return jsonify({'success': False, 'error': 'No valid keys'}), 400
    result = mgr.set_config_batch('ads', cfg, coerce=True)
    if result.get('errors'):
        return jsonify({'success': True, 'warning': result['errors'], 'data': {'saved': True}})
    return jsonify({'success': True, 'data': {'saved': True}})


# ============================================================
# 站点解析辅助
# =========================================================

def _resolve_site_key_from_host():
    """根据请求 Host 从 site_domains 表解析当前子域名作为 site_key"""
    host = request.headers.get('Host', '').split(':')[0].lower()
    # 本地 / 空 / 超长 Host 一律回落默认（Host 头最长 253 字符，防止 DoS）
    if not host or len(host) > 253 or host.startswith('127.') or host == 'localhost':
        return 'default'
    try:
        from models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT subdomain FROM site_domains WHERE full_domain=%s AND is_published=1",
                (host,)
            ).fetchone()
            if row and row['subdomain']:
                return row['subdomain']
    except Exception as e:
        current_app.logger.warning('[Ads] _resolve_site_key_from_host failed host=%s: %s', host, e)
    return 'default'


# ============================================================
# 公开广告渲染 API
# ============================================================

@ads_bp.route('/api/v1/ads', methods=['GET'])
def public_ads():
    """公开端点 — 前端页面调用以渲染广告
    GET /admin/ads/api/v1/ads?page=*&position=sidebar&site_key=default&zone_id=0&limit=50
    返回当前页、位置、站点下所有活跃且符合投放条件的广告（limit 上限 200）
    """
    page = request.args.get('page', '*', type=str).strip()
    position = request.args.get('position', '', type=str).strip()
    site_key = request.args.get('site_key', '').strip() or _resolve_site_key_from_host()
    zone_id = request.args.get('zone_id', type=int)
    limit = request.args.get('limit', 50, type=int)
    limit = max(1, min(limit, 200))

    # 公开端点限流（IP 维度），防高频拉取压垮数据库
    client = request.remote_addr or 'unknown'
    if not _rate_limit(f'ads_public:{client}', 30):
        return jsonify({'success': False, 'error': _('Too many requests')}), 429

    from plugins.ads.models import get_ads_db
    where = ['is_active=1']
    params = []

    if position:
        where.append('position=%s')
        params.append(position)
    if site_key:
        where.append('site_key=%s')
        params.append(site_key)
    if zone_id is not None:
        where.append('zone_id=%s')
        params.append(zone_id)

    # 页面匹配：精确页面或通配 *
    where.append('(page=%s OR page=%s)')
    params.extend([page, '*'])

    # 时间过滤：当前时间需在 schedule_start 与 schedule_end 之间（若设置）
    now = _now_str()
    where.append("(schedule_start='' OR schedule_start<=%s)")
    params.append(now)
    where.append("(schedule_end='' OR schedule_end>=%s)")
    params.append(now)

    with get_ads_db() as conn:
        rows = conn.execute(
            f'SELECT * FROM ad_placements WHERE {" AND ".join(where)} ORDER BY sort_order, id LIMIT %s',
            params + [limit]
        ).fetchall()

    return jsonify({
        'success': True,
        'data': [_ad_row_to_dict(r) for r in rows]
    })
