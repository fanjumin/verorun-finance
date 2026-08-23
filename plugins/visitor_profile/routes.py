#!/usr/bin/env python3
"""
visitor_profile/routes.py — 访客画像管理后台 Flask Blueprint
==============================================================
提供:
  - 页面路由: /admin/visitor_profile/          — 管理后台仪表盘
             /admin/visitor_profile/<visitor_id> — 访客画像详情页
  - API 路由: /admin/visitor_profile/api/v1/visitors           — 访客列表
             /admin/visitor_profile/api/v1/visitors/<visitor_id> — 单访客详情
             /admin/visitor_profile/api/v1/events               — 事件日志
             /admin/visitor_profile/api/v1/stats                — 统计
  - 采集路由: /admin/visitor_profile/ingest    — 前端埋点 SDK 上报（免鉴权）
  - 静态文件: /admin/visitor_profile/static/<path>

鉴权: 参考 analytics/routes.py — before_request + JWT 验证。
     静态文件与采集接口免鉴权（tracker.js 需要无认证上报）。
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

from flask import (Blueprint, request, jsonify, render_template,
                   send_from_directory, redirect, g)

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.event_bus import get_event_bus
from plugin_manager.logger import get_plugin_logger

from . import models as vm

logger = get_plugin_logger('visitor_profile')

VISITOR_ACTIVITY_EVENT = 'visitor.activity'

visitor_profile_bp = Blueprint(
    'visitor_profile', __name__,
    url_prefix='/admin/visitor_profile',
    template_folder='templates',
    static_folder='static',
    static_url_path='/admin/visitor_profile/static',
)

# i18n 桥接 — 默认绑定全局 i18n._，插件注入时可覆盖（init_i18n）
from i18n import _ as _i18n
_t = _i18n


def init_i18n(t_func):
    global _t
    _t = t_func


# ─── 鉴权（参考 analytics/routes.py） ──────────────────────────────────────

_AUTH_EXEMPT_PATHS = [
    '/admin/visitor_profile/static/',
    '/admin/visitor_profile/ingest',
]


@visitor_profile_bp.before_request
def check_auth():
    """所有路由需要管理员 JWT 验证（除静态文件与采集 API）。"""
    path = request.path
    for exempt in _AUTH_EXEMPT_PATHS:
        if path.startswith(exempt):
            return None

    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.args.get('token')
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        if request.is_json or path.startswith('/admin/visitor_profile/api/'):
            return jsonify({'success': False, 'error': _t('Unauthorized')}), 401
        # 直接访问无 token → 跳转登录页
        return redirect('/admin/login')
    return None


# ─── 静态文件 ───────────────────────────────────────────────────────────────

@visitor_profile_bp.route('/static/<path:filename>')
def static_files(filename):
    """提供插件静态文件（tracker.js 等）。"""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), 'static'), filename)


# ─── 页面路由 ───────────────────────────────────────────────────────────────

@visitor_profile_bp.route('/')
def dashboard_page():
    """渲染管理后台仪表盘（iframe 独立页面，§12.11）。"""
    translations = {}
    try:
        from i18n import get_lang
        locale = get_lang()
    except Exception:
        locale = 'zh-CN'
    try:
        from plugin_manager.base import _load_plugin_yaml
        translations = _load_plugin_yaml(
            'visitor_profile',
            os.path.join(os.path.dirname(__file__), 'i18n')).get(locale, {})
    except Exception:
        translations = {}
    return render_template('admin_dashboard.html',
                           translations=translations,
                           g=g)


@visitor_profile_bp.route('/<visitor_id>')
def visitor_detail_page(visitor_id):
    """渲染单访客画像详情页。"""
    translations = {}
    try:
        from i18n import get_lang
        locale = get_lang()
    except Exception:
        locale = 'zh-CN'
    try:
        from plugin_manager.base import _load_plugin_yaml
        translations = _load_plugin_yaml(
            'visitor_profile',
            os.path.join(os.path.dirname(__file__), 'i18n')).get(locale, {})
    except Exception:
        translations = {}
    return render_template('visitor_detail.html',
                           translations=translations,
                           visitor_id=visitor_id,
                           g=g)


# ─── 采集 API（前端埋点 SDK 上报，免鉴权） ─────────────────────────────────

@visitor_profile_bp.route('/ingest', methods=['POST'])
def ingest_event():
    """接收前端 tracker 上报的访客行为事件（支持单条或批量）。

    Request body:
    {
        "visitor_id": "vr_xxx",
        "events": [
            {
                "event_type": "page_view",
                "page_url": "https://example.com/pricing",
                "page_title": "Pricing",
                "element_id": null,
                "element_text": null,
                "event_data": {"scroll_depth": 0.85},
                "session_id": "sess_xxx",
                "timestamp": "2026-08-11T12:00:00Z"
            }
        ]
    }
    """
    try:
        data = request.get_json(force=True) or {}
        visitor_id = data.get('visitor_id') or str(uuid.uuid4())
        events = data.get('events', [])
        if not events:
            return jsonify({'error': _t('No events provided')}), 400

        # 单条事件（兼容直接上报单事件对象）
        if isinstance(events, dict):
            events = [events]

        # 请求级限流（防滥用，简单 IP 计数）
        if not _rate_limited(request.remote_addr or ''):
            return jsonify({'error': _t('Too many requests')}), 429

        bus = get_event_bus()
        queued = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            payload = {
                'visitor_id': visitor_id,
                'event_type': event.get('event_type', 'custom'),
                'page_url': event.get('page_url'),
                'page_title': event.get('page_title'),
                'element_id': event.get('element_id'),
                'element_text': event.get('element_text'),
                'event_data': event.get('event_data', {}),
                'session_id': event.get('session_id'),
                'timestamp': event.get('timestamp'),
                'device_fingerprint': event.get('device_fingerprint'),
                'user_agent': event.get('user_agent'),
                'referrer': event.get('referrer'),
                'utm_source': event.get('utm_source'),
                'utm_medium': event.get('utm_medium'),
                'utm_campaign': event.get('utm_campaign'),
                'user_id': event.get('user_id'),
            }
            bus.emit(VISITOR_ACTIVITY_EVENT, event_data=payload)
            queued += 1

        return jsonify({'ok': True, 'visitor_id': visitor_id,
                        'events_queued': queued})
    except Exception as e:
        logger.error('Ingest failed: %s', e)
        return jsonify({'error': str(e)}), 500


# ─── 进程内 IP 限流（无第三方依赖） ─────────────────────────────────────────

_RATE_WINDOW = 60
_RATE_MAX = 600
_RATE_MAP = {}


def _rate_limited(ip: str) -> bool:
    """滑动窗口限流：True=放行，False=超限。"""
    if _RATE_MAX <= 0:
        return True
    now = datetime.now()
    key = ip or 'unknown'
    q = _RATE_MAP.setdefault(key, [])
    cutoff = now - timedelta(seconds=_RATE_WINDOW)
    while q and q[0] < cutoff:
        q.pop(0)
    if len(q) >= _RATE_MAX:
        return False
    q.append(now)
    if len(_RATE_MAP) > 10000:
        for k in [k for k, v in _RATE_MAP.items() if not v][:2000]:
            _RATE_MAP.pop(k, None)
    return True


# ─── API: 访客列表 ─────────────────────────────────────────────────────────

@visitor_profile_bp.route('/api/v1/visitors', methods=['GET'])
def api_list_visitors():
    """访客列表查询（支持 keyword 搜索 + 分页）。"""
    try:
        keyword = request.args.get('keyword', '')
        page = max(int(request.args.get('page', 1)), 1)
        page_size = min(max(int(request.args.get('page_size', 20)), 1), 100)
        offset = (page - 1) * page_size

        visitors = vm.VisitorModel.list_visitors(
            limit=page_size, offset=offset, keyword=keyword)
        total = len(vm.VisitorModel.list_visitors(limit=100000, offset=0, keyword=keyword))
        return jsonify({
            'success': True,
            'data': visitors,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
            },
        })
    except Exception as e:
        logger.error('List visitors failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── API: 单访客画像详情 ───────────────────────────────────────────────────

@visitor_profile_bp.route('/api/v1/visitors/<visitor_id>', methods=['GET'])
def api_get_visitor(visitor_id):
    """单访客画像详情（基础信息 + 事件 + 记忆）。"""
    try:
        visitor = vm.VisitorModel.get_by_id(visitor_id)
        if not visitor:
            return jsonify({'success': False,
                            'error': _t('Visitor not found')}), 404

        events = vm.EventLogModel.get_by_visitor(visitor_id, limit=50)
        memories = vm.MemoryModel.get_active_by_visitor(visitor_id, limit=20)

        # JSONB 字段转 dict/list 返回
        for v in (visitor,):
            for k in ('profile_summary', 'tags'):
                if isinstance(v.get(k), str):
                    try:
                        v[k] = json.loads(v[k])
                    except Exception:
                        pass

        return jsonify({
            'success': True,
            'visitor': visitor,
            'events': events,
            'memories': memories,
        })
    except Exception as e:
        logger.error('Get visitor failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── API: 事件日志查询 ─────────────────────────────────────────────────────

@visitor_profile_bp.route('/api/v1/events', methods=['GET'])
def api_list_events():
    """事件日志查询（支持按 visitor_id 过滤 + 分页）。"""
    try:
        visitor_id = request.args.get('visitor_id', '')
        page = max(int(request.args.get('page', 1)), 1)
        page_size = min(max(int(request.args.get('page_size', 50)), 1), 200)
        offset = (page - 1) * page_size

        if visitor_id:
            events = vm.EventLogModel.get_by_visitor(
                visitor_id, limit=page_size)
        else:
            with vm.get_db() as conn:
                rows = conn.execute('''
                    SELECT id, visitor_id, event_type, page_url, page_title,
                           event_data, session_id, client_ts, server_ts
                    FROM event_log
                    ORDER BY server_ts DESC
                    LIMIT %s OFFSET %s
                ''', (page_size, offset)).fetchall()
                events = [dict(r) for r in rows]

        return jsonify({'success': True, 'data': events})
    except Exception as e:
        logger.error('List events failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ─── API: 仪表盘统计 ───────────────────────────────────────────────────────

@visitor_profile_bp.route('/api/v1/stats', methods=['GET'])
def api_stats():
    """仪表盘统计数据（总访客 / 24h 事件 / 24h 画像 / 平均耗时 / 任务状态）。"""
    try:
        total_visitors = vm.VisitorModel.count()
        events_24h = vm.VisitorModel.count_events_24h()
        profiles_24h = vm.MemoryModel.count_created_24h()
        avg_time_ms = vm.ExtractionTaskModel.avg_processing_time_24h()
        task_stats = vm.ExtractionTaskModel.stats()

        # Top 意图（profile_summary->>'primary_intent'）
        top_intents = []
        try:
            with vm.get_db() as conn:
                rows = conn.execute('''
                    SELECT profile_summary->>'primary_intent' AS intent,
                           COUNT(*) AS cnt
                    FROM visitors
                    WHERE profile_summary ? 'primary_intent'
                    GROUP BY intent
                    ORDER BY cnt DESC
                    LIMIT 10
                ''').fetchall()
                top_intents = [dict(r) for r in rows]
        except Exception:
            pass

        return jsonify({
            'success': True,
            'total_visitors': total_visitors,
            'events_24h': events_24h,
            'profiles_24h': profiles_24h,
            'avg_extraction_time_ms': avg_time_ms,
            'task_stats': task_stats,
            'top_intents': top_intents,
        })
    except Exception as e:
        logger.error('Stats failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
