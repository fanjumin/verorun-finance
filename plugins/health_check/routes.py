#!/usr/bin/env python3
"""
VeroRun Intelligent (verorun.com / verorun.cn)
Copyright (c) 2026 Fan Jumin. All Rights Reserved.

Health Check — Flask API Routes
=================================
Provides REST API for health checks + admin dashboard page.

Registration:
    from health_check.routes import health_bp
    app.register_blueprint(health_bp, url_prefix='/admin/health')

API Endpoints:
    GET   /admin/health/              — Admin dashboard page
    GET   /admin/health/api/status    — Current status dashboard
    POST  /admin/health/api/run       — Trigger manual check
    GET   /admin/health/api/history   — Check history list
    GET   /admin/health/api/history/<run_id> — Specific check details
    GET   /admin/health/api/checks    — Check items list
    PUT   /admin/health/api/checks/<id> — Update check item config
    GET   /admin/health/api/trend     — Health trend data
    GET   /admin/health/api/alerts    — Alert history
    POST  /admin/health/api/alerts/read — Mark alerts as read
    GET   /admin/health/api/export    — Export check report as JSON
"""

import os, sys, json, time, threading
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.append(os.path.join(BASE_DIR, '..'))
_t = lambda s: s
def init_i18n(t_func):
    global _t
    _t = t_func

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('health_check')
except ImportError:
    import logging
    _logger = logging.getLogger('health_check')

from . import models as m
from .checkers import CheckerRegistry
from .alerter import evaluate_and_alert
from .discovery import DiscoveryReporter  # noqa: F401 — ensures @register decorators fire
from .metrics import generate_metrics

health_bp = Blueprint('health', __name__,
                      url_prefix='/admin/health',
                      template_folder='templates',
                      static_folder='static',
                      static_url_path='/admin/health/static')


@health_bp.context_processor
def inject_i18n():
    """Inject plugin _t into template context"""
    return {'_t': _t}


# ─── Authentication Helper ──────────────────────────────────────────────────

def _require_admin():
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token') or request.headers.get('X-Token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return None
    return payload


# ─── Check Execution Engine ─────────────────────────────────────────────────

def run_health_check(trigger_type='manual', trigger_info='', check_keys=None):
    """
    Execute a complete health check.
    check_keys: If specified, only run specific check items (e.g. ['core_api', 'database'])
    Returns run_id
    """
    with m.get_db() as conn:
        # Get all active check items
        if check_keys:
            rows = conn.execute(
                'SELECT * FROM health_checks WHERE is_active=1 AND check_key IN ({})'.format(
                    ','.join('%s' for _ in check_keys)
                ), check_keys
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM health_checks WHERE is_active=1 ORDER BY sort_order'
            ).fetchall()

        total = len(rows)
        # Create check run batch
        cur = conn.execute(
            "INSERT INTO check_runs (trigger_type, trigger_info, total_checks, status) VALUES (%s,%s,%s,'running') RETURNING id",
            (trigger_type, trigger_info, total)
        )
        run_id = cur.fetchone()['id']
        conn.commit()

    # Execute checks one by one
    results = []
    passed = warnings = errors = 0
    total_duration = 0

    for row in rows:
        check = dict(row)
        config = json.loads(check.get('config', '{}'))
        checker = CheckerRegistry.get_instance(check['check_key'], config)

        if not checker:
            # No corresponding checker, mark as warning
            result = {
                'check_id': check['id'],
                'check_key': check['check_key'],
                'check_name': _t(check['name']),
                'category': check['category'],
                'status': 'warning',
                'response_time_ms': 0,
                'message': _t('No checker implementation for: {key}').format(key=check['check_key']),
                'detail': '{}',
            }
        else:
            try:
                cr = checker.run()
                result = {
                    'check_id': check['id'],
                    'check_key': check['check_key'],
                    'check_name': _t(check['name']),
                    'category': check['category'],
                    'status': cr.status,
                    'response_time_ms': cr.response_time_ms,
                    'message': cr.message,
                    'detail': json.dumps(cr.detail, ensure_ascii=False),
                }
            except Exception as e:
                result = {
                    'check_id': check['id'],
                    'check_key': check['check_key'],
                    'check_name': _t(check['name']),
                    'category': check['category'],
                    'status': 'error',
                    'response_time_ms': 0,
                    'message': _t('Checker exception: {err}').format(err=e),
                    'detail': json.dumps({'error': str(e)}, ensure_ascii=False),
                }

        total_duration += result['response_time_ms']
        if result['status'] == 'passed':
            passed += 1
        elif result['status'] == 'warning':
            warnings += 1
        else:
            errors += 1
        results.append(result)

    # Save results to database
    with m.get_db() as conn:
        for r in results:
            conn.execute(
                'INSERT INTO check_history (run_id, check_id, check_key, check_name, category, '
                'status, response_time_ms, message, detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (run_id, r['check_id'], r['check_key'], r['check_name'], r['category'],
                 r['status'], r['response_time_ms'], r['message'], r['detail'])
            )
        # Update run batch
        conn.execute(
            'UPDATE check_runs SET passed=%s, warnings=%s, errors=%s, duration_ms=%s, '
            "status='completed', summary=%s WHERE id=%s",
            (passed, warnings, errors, total_duration,
             f'✅ {passed} ⚠️ {warnings} ❌ {errors}', run_id)
        )
        conn.commit()

    # Alert evaluation
    try:
        evaluate_and_alert(run_id, results)
    except Exception as e:
        _logger.error('Alert evaluation failed: %s', e)

    # Update daily trend
    _update_daily_trend()

    return run_id


def _update_daily_trend():
    """Update daily health trend statistics"""
    today = datetime.now().strftime('%Y-%m-%d')
    with m.get_db() as conn:
        stats = conn.execute(
            "SELECT COUNT(*) as total, "
            "COALESCE(SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END), 0) as passed, "
            "COALESCE(SUM(CASE WHEN status='warning' THEN 1 ELSE 0 END), 0) as warnings, "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0) as errors, "
            "COALESCE(AVG(response_time_ms), 0) as avg_ms "
            "FROM check_history WHERE date(checked_at)=%s",
            (today,)
        ).fetchone()

        if stats and stats['total'] > 0:
            total = stats['total']
            score = 100.0
            if total > 0:
                score = ((stats['passed'] * 100) + (stats['warnings'] * 60)) / total
                score = round(score, 1)

            conn.execute(
                "INSERT INTO health_trend (date, total_checks, passed, warnings, errors, avg_response_ms, health_score) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (date) DO UPDATE SET total_checks=EXCLUDED.total_checks, passed=EXCLUDED.passed, warnings=EXCLUDED.warnings, errors=EXCLUDED.errors, avg_response_ms=EXCLUDED.avg_response_ms, health_score=EXCLUDED.health_score",
                (today, stats['total'], stats['passed'], stats['warnings'], stats['errors'],
                 int(stats['avg_ms']), score)
            )
            conn.commit()


# ═════════════════════════════════════════════════════════════════════════
# API Endpoints
# ═════════════════════════════════════════════════════════════════════════

@health_bp.route('/')
def health_page():
    """Admin dashboard page (iframe embed mode, same as analytics)"""
    admin = _require_admin()
    if not admin:
        return '', 401
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.cookies.get('sso_token') or request.headers.get('X-Token') or ''
    return render_template('health.html', sso_token=token)


@health_bp.route('/api/status')
def api_status():
    """Get current health check status (latest run results + overview statistics)"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    latest = m.get_latest_status()
    unread_alerts = m.get_unread_alert_count()

    # Status statistics by category
    categories = {}
    if latest and latest.get('items'):
        for item in latest['items']:
            cat = item.get('category', 'other')
            if cat not in categories:
                categories[cat] = {'total': 0, 'passed': 0, 'warning': 0, 'error': 0}
            categories[cat]['total'] += 1
            categories[cat][item['status']] += 1

    return jsonify({
        'success': True,
        'data': {
            'latest_run': latest,
            'categories': categories,
            'unread_alerts': unread_alerts,
        }
    })


@health_bp.route('/api/discovery/status')
def api_discovery_status():
    """Run resource discovery and return results."""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    reporter = DiscoveryReporter()

    # Try to get Flask app for endpoint scanning
    flask_app = None
    try:
        from flask import current_app
        flask_app = current_app._get_current_object()
    except (RuntimeError, AttributeError):
        pass

    result = reporter.run(flask_app=flask_app)

    return jsonify({
        'success': True,
        'data': result,
        'summary': reporter.summary_text(result),
    })


@health_bp.route('/api/run', methods=['POST'])
def api_run():
    """Trigger manual health check"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    check_keys = request.json.get('checks') if request.is_json else None

    # Run asynchronously (avoid client waiting)
    def _async_run():
        run_health_check(trigger_type='manual', trigger_info=f'admin:{admin["user_id"]}',
                         check_keys=check_keys)

    t = threading.Thread(target=_async_run, daemon=True)
    t.start()

    return jsonify({'success': True, 'message': _t('Health check started, results will appear shortly')})


@health_bp.route('/api/history')
def api_history():
    """Health check history list"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    page = request.args.get('page', 1, type=int)
    limit = min(request.args.get('limit', 20, type=int), 100)
    offset = (page - 1) * limit
    trigger_filter = request.args.get('trigger', '')
    since = request.args.get('since', '')  # e.g. '-1 hour', '-24 hours'

    with m.get_db() as conn:
        where = ''
        params = []
        if trigger_filter:
            where = 'WHERE trigger_type=?'
            params.append(trigger_filter)
        if since:
            cond = 'WHERE' if not where else 'AND'
            where += f" {cond} created_at >= NOW() + %s::INTERVAL"
            params.append(since)

        total = conn.execute(
            f'SELECT COUNT(*) as c FROM check_runs {where}', params
        ).fetchone()['c']

        rows = conn.execute(
            f'SELECT * FROM check_runs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?',
            params + [limit, offset]
        ).fetchall()

    return jsonify({
        'success': True,
        'data': {
            'total': total,
            'page': page,
            'limit': limit,
            'runs': [dict(r) for r in rows],
        }
    })


@health_bp.route('/api/history/<int:run_id>')
def api_history_detail(run_id):
    """Detailed results of a specific health check run"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    with m.get_db() as conn:
        run = conn.execute('SELECT * FROM check_runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return jsonify({'success': False, 'error': _t('Health check record not found')}), 404
        items = m.get_history_for_run(run_id)

    return jsonify({
        'success': True,
        'data': {
            'run': dict(run),
            'items': items,
        }
    })


@health_bp.route('/api/checks')
def api_checks():
    """Get check items list"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    with m.get_db() as conn:
        rows = conn.execute('SELECT * FROM health_checks ORDER BY sort_order').fetchall()

    return jsonify({
        'success': True,
        'data': {
            'checks': [dict(r) for r in rows],
        }
    })


@health_bp.route('/api/checks/<int:check_id>', methods=['PUT', 'DELETE'])
def api_update_check(check_id):
    """Update or delete check item configuration"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    if request.method == 'DELETE':
        with m.get_db() as conn:
            conn.execute('DELETE FROM health_checks WHERE id=?', (check_id,))
            conn.commit()
        return jsonify({'success': True, 'message': 'Deleted'})

    data = request.json
    if not data:
        return jsonify({'success': False, 'error': _t('Invalid data')}), 400

    with m.get_db() as conn:
        check = conn.execute('SELECT * FROM health_checks WHERE id=?', (check_id,)).fetchone()
        if not check:
            return jsonify({'success': False, 'error': _t('Check item not found')}), 404

        updates = []
        params = []
        for field in ('is_active', 'name', 'description', 'severity', 'sort_order'):
            if field in data:
                updates.append(f'{field}=?')
                params.append(data[field])
        if 'config' in data and isinstance(data['config'], dict):
            updates.append('config=?')
            params.append(json.dumps(data['config'], ensure_ascii=False))

        updates.append("updated_at=NOW()")
        params.append(check_id)

        conn.execute(
            f'UPDATE health_checks SET {", ".join(updates)} WHERE id=?',
            params
        )
        conn.commit()

    return jsonify({'success': True, 'message': _t('Updated')})


@health_bp.route('/api/trend')
def api_trend():
    """Health trend data"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    days = request.args.get('days', 7, type=int)
    if days not in (7, 14, 30):
        days = 7

    trend = m.get_health_trend(days)

    return jsonify({
        'success': True,
        'data': {
            'days': days,
            'trend': trend,
        }
    })


@health_bp.route('/api/checkers/registry')
def api_checkers_registry():
    """Get all registered checker metadata (for management UI)"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    registered = CheckerRegistry.list_registered()

    # Get check item configuration from current DB
    with m.get_db() as conn:
        db_checks = conn.execute('SELECT check_key, is_active, config, sort_order FROM health_checks').fetchall()
    db_map = {r['check_key']: dict(r) for r in db_checks}

    # Merge information
    merged = []
    for info in registered:
        ck = info['check_key']
        db_info = db_map.get(ck, {})
        merged.append({
            **info,
            'is_active': db_info.get('is_active', 1) if db_info else 0,
            'in_db': ck in db_map,
        })

    # Also mark those not in DB but registered
    registered_keys = {r['check_key'] for r in registered}
    for ck, db_info in db_map.items():
        if ck not in registered_keys:
            merged.append({
                'check_key': ck,
                'name': db_info.get('name', ck),
                'category': 'unknown',
                'severity': 'warning',
                'description': 'Exists in database but has no corresponding checker implementation',
                'in_db': True,
                'is_active': db_info.get('is_active', 0),
            })

    return jsonify({
        'success': True,
        'data': {
            'registered': merged,
            'total': len(merged),
            'registry_count': len(registered),
        }
    })


@health_bp.route('/api/checkers/register', methods=['POST'])
def api_register_check():
    """Register a new check item from admin (write to DB + check if checker class is already loaded)"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    check_key = data.get('check_key', '').strip()
    name = data.get('name', check_key)
    category = data.get('category', 'system')
    severity = data.get('severity', 'warning')

    if not check_key:
        return jsonify({'success': False, 'error': _t('check_key is required')}), 400

    checker_class = CheckerRegistry.get(check_key)
    if checker_class:
        # Use metadata defined in the checker class
        inst = checker_class({})
        name = name or inst.get_name()
        category = category or inst.get_category()
        severity = severity or inst.get_severity()
        config = json.dumps(inst.get_config_defaults(), ensure_ascii=False)
    else:
        # Only check_key, no corresponding Python checker class
        config = '{}'

    with m.get_db() as conn:
        existing = conn.execute('SELECT id FROM health_checks WHERE check_key=?', (check_key,)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': f'Check item {check_key} already exists (ID={existing["id"]})'})

        # Get max sort_order
        max_order = conn.execute('SELECT COALESCE(MAX(sort_order),0)+10 as o FROM health_checks').fetchone()['o']

        cur = conn.execute(
            'INSERT INTO health_checks (check_key, name, category, description, config, severity, sort_order, is_active) '
            'VALUES (?,?,?,?,?,?,?,1) RETURNING id',
            (check_key, name, category, data.get('description', ''), config, severity, max_order)
        )
        new_id = cur.fetchone()['id']
        conn.commit()

    return jsonify({
        'success': True,
        'message': _t('Check item {name} added').format(name=name),
        'data': {'id': new_id, 'check_key': check_key, 'has_checker': checker_class is not None}
    })


# ─── Prometheus Metrics Endpoint ─────────────────────────────────────────────

@health_bp.route('/api/metrics')
def api_metrics():
    """Prometheus-compatible metrics endpoint (no auth, for scraper access)."""
    from flask import Response
    return Response(generate_metrics(), mimetype='text/plain; charset=utf-8')


@health_bp.route('/api/alerts')
def api_alerts():
    """Alert history"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    alerts = m.get_alerts(limit=100)

    return jsonify({
        'success': True,
        'data': {
            'alerts': alerts,
        }
    })


@health_bp.route('/api/alerts/read', methods=['POST'])
def api_alerts_read():
    """Mark alerts as read"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    alert_id = data.get('alert_id')

    with m.get_db() as conn:
        # ── Auto-upgrade: check BEFORE marking as read ──
        _auto_upgrade_stale_alerts(conn)

        if alert_id:
            conn.execute('UPDATE alert_history SET is_read=1 WHERE id=?', (alert_id,))
        else:
            conn.execute('UPDATE alert_history SET is_read=1')

        conn.commit()

    return jsonify({'success': True})


# ─── Silences API ─────────────────────────────────────────────────────────────

@health_bp.route('/api/alerts/silences')
def api_silences_list():
    """List all silence windows"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    with m.get_db() as conn:
        silences = conn.execute(
            'SELECT * FROM alert_silences ORDER BY created_at DESC LIMIT 50'
        ).fetchall()
    return jsonify({
        'success': True,
        'data': {
            'silences': [dict(s) for s in silences]
        }
    })


@health_bp.route('/api/alerts/silences', methods=['POST'])
def api_silences_create():
    """Create a new silence window"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    check_key = data.get('check_key', '*')
    starts_at = data.get('starts_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    ends_at = data.get('ends_at')
    reason = data.get('reason', '')
    created_by = admin.get('username', admin.get('display_name', 'admin'))

    if not ends_at:
        return jsonify({'success': False, 'error': 'ends_at is required'}), 400

    with m.get_db() as conn:
        conn.execute(
            'INSERT INTO alert_silences (check_key, starts_at, ends_at, reason, created_by) '
            'VALUES (?,?,?,?,?)',
            (check_key, starts_at, ends_at, reason, created_by)
        )
        conn.commit()

    return jsonify({'success': True, 'message': 'Silence window created'})


@health_bp.route('/api/alerts/silences/<int:silence_id>', methods=['DELETE'])
def api_silences_delete(silence_id):
    """Delete a silence window"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    with m.get_db() as conn:
        conn.execute('DELETE FROM alert_silences WHERE id=?', (silence_id,))
        conn.commit()

    return jsonify({'success': True, 'message': 'Silence window removed'})


# ─── Auto-upgrade helper ─────────────────────────────────────────────────────

def _auto_upgrade_stale_alerts(conn):
    """Upgrade P3 alerts that have been unread for > 30 minutes to P1.
    
    This is called when admin marks alerts as read — checks for any
    P3 alerts that have been sitting unread and escalates them.
    """
    threshold = (datetime.now() - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
    stale = conn.execute(
        "SELECT id, check_key, check_name FROM alert_history "
        "WHERE alert_level='P3' AND is_read=0 AND created_at <= %s",
        (threshold,)
    ).fetchall()

    for row in stale:
        conn.execute(
            "UPDATE alert_history SET alert_level='P1' WHERE id=%s",
            (row['id'],)
        )
        _logger.warning('Auto-upgraded P3->P1: %s (%s)', row["check_key"], row["check_name"])


@health_bp.route('/api/export')
def api_export():
    """Export health check report as JSON"""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    run_id = request.args.get('run_id', type=int)
    recent = request.args.get('recent', '1', type=str)

    with m.get_db() as conn:
        if run_id:
            run = conn.execute('SELECT * FROM check_runs WHERE id=?', (run_id,)).fetchone()
        else:
            run = conn.execute(
                "SELECT * FROM check_runs WHERE status='completed' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        if not run:
            return jsonify({'success': False, 'error': _t('No health report available')}), 404

        run = dict(run)
        items = conn.execute(
            'SELECT * FROM check_history WHERE run_id=%s ORDER BY category, id',
            (run['id'],)
        ).fetchall()
        run['items'] = [dict(i) for i in items]

    return jsonify({
        'success': True,
        'data': {
            'export_time': datetime.now().isoformat(),
            'platform': '',
            'report': run,
        }
    })


# ─── Fix Execution API ────────────────────────────────────────────────────────

@health_bp.route('/api/fix', methods=['POST'])
def api_fix():
    """
    Execute fix operations (select from fix_suggestions in health check results and execute).
    Request body:
    {
        "run_id": 123,                  // Health check run ID
        "check_key": "media_integrity", // Check item key
        "indices": [0, 1, 2],           // Indices in fix_suggestions (optional, all executed if not provided)
        "confirm": true                 // Confirm execution (must be true)
    }
    """
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    run_id = data.get('run_id')
    check_key = data.get('check_key', '')
    selected_indices = data.get('indices')
    confirm = data.get('confirm', False)

    if not confirm:
        return jsonify({'success': False, 'error': 'Please confirm execution (confirm: true)'}), 400
    if not run_id or not check_key:
        return jsonify({'success': False, 'error': 'run_id and check_key are required'}), 400

    # Read the check_history record for this run from the database
    with m.get_db() as conn:
        history_row = conn.execute(
            'SELECT * FROM check_history WHERE run_id=%s AND check_key=%s',
            (run_id, check_key)
        ).fetchone()

    if not history_row:
        return jsonify({'success': False, 'error': _t('Corresponding check result not found')}), 404

    history = dict(history_row)
    try:
        detail = json.loads(history.get('detail', '{}'))
    except (json.JSONDecodeError, TypeError):
        detail = {}

    fix_suggestions = detail.get('fix_suggestions', [])
    if not fix_suggestions:
        return jsonify({'success': True, 'message': _t('No items need fixing')})

    # Filter suggestions to execute
    if selected_indices is not None:
        try:
            to_apply = [fix_suggestions[i] for i in selected_indices]
        except (IndexError, TypeError):
            return jsonify({'success': False, 'error': _t('Invalid index')}), 400
    else:
        to_apply = fix_suggestions

    # Execute fixes
    from .checkers import FixSuggestion
    applied = 0
    errors = []
    with m.get_db() as conn:
        for s_data in to_apply:
            try:
                sug = FixSuggestion(
                    action=s_data.get('action', 'clear_field'),
                    reason=s_data.get('reason', ''),
                    params=s_data.get('params', {}),
                    record_type=s_data.get('record_type', ''),
                )
                ok = FixSuggestion.apply_fix(conn, sug)
                if ok:
                    applied += 1
                else:
                    errors.append(f"Action={sug.action}: Execution failed")
            except Exception as e:
                errors.append(f"Action={s_data.get('action')}: {e}")
        conn.commit()

    return jsonify({
        'success': True,
        'data': {
            'applied': applied,
            'total': len(to_apply),
            'errors': errors,
            'run_id': run_id,
        },
        'message': _t('Fixed {applied}/{total} records').format(applied=applied, total=len(to_apply))
    })


# ═══════════════════════════════════════════════════════════════════════════
# Internal Link Scan & Report
# ═══════════════════════════════════════════════════════════════════════════

@health_bp.route('/api/links/scan', methods=['POST'])
def api_links_scan():
    """
    Trigger an internal link scan and return results immediately.
    Request body (optional):
    {
        "max_urls": 50,
        "timeout": 5
    }
    """
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    from .checkers import InternalLinkChecker

    checker = InternalLinkChecker()
    checker.config = {
        'max_urls': data.get('max_urls', checker.config_defaults.get('max_urls', 50)),
        'timeout': data.get('timeout', checker.config_defaults.get('timeout', 5)),
    }

    result = checker.check()

    response_data = {
        'status': result.status,
        'elapsed_ms': result.elapsed_ms,
        'message': result.message,
        'detail': result.detail,
        'success': True,
    }
    return jsonify(response_data)


@health_bp.route('/api/links/report', methods=['GET'])
def api_links_report():
    """
    Get the latest link check report from check_history.
    Query params:
        check_key (optional, default: internal_links)
        limit (optional, default: 1 — most recent run)
    """
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    check_key = request.args.get('check_key', 'internal_links')
    limit = request.args.get('limit', 1, type=int)

    with m.get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM check_history WHERE check_key=%s '
            'ORDER BY run_id DESC LIMIT %s',
            (check_key, limit)
        ).fetchall()

    if not rows:
        return jsonify({
            'success': True,
            'data': None,
            'message': _t('No reports found for this check'),
        })

    reports = []
    for r in rows:
        d = dict(r)
        try:
            d['detail'] = json.loads(d.get('detail', '{}'))
        except (json.JSONDecodeError, TypeError):
            d['detail'] = {}
        reports.append(d)

    return jsonify({'success': True, 'data': reports})


# ═══════════════════════════════════════════════════════════════════════════
# AI-Powered Analysis & Fix
# ═══════════════════════════════════════════════════════════════════════════

@health_bp.route('/api/ai-analyze', methods=['POST'])
def api_ai_analyze():
    """
    Send health check results to LLM for analysis.
    Returns root cause analysis + repair suggestions.

    Request body:
    {
        "run_id": 123,
        "check_key": "internal_links",
        "detail": {}  // Optional — if provided, uses this instead of fetching from DB
    }
    """
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    run_id = data.get('run_id')
    check_key = data.get('check_key', '')
    detail_override = data.get('detail')

    # ── 全局模式：无 check_key → 分析最近一次 run 的全部检查结果 ──────
    if not check_key and not detail_override:
        with m.get_db() as conn:
            if run_id:
                latest = conn.execute(
                    'SELECT id, run_type FROM health_runs WHERE id=%s LIMIT 1',
                    (run_id,)
                ).fetchone()
            else:
                latest = conn.execute(
                    'SELECT id, run_type FROM health_runs ORDER BY id DESC LIMIT 1'
                ).fetchone()
            if not latest:
                return jsonify({'success': False, 'error': _t('No health run found')}), 404
            rows = conn.execute(
                'SELECT check_key, status, message, detail FROM check_history '
                'WHERE run_id=%s ORDER BY id ASC',
                (latest['id'],)
            ).fetchall()
        results = []
        for row in rows:
            detail_raw = row['detail'] or '{}'
            detail = json.loads(detail_raw) if isinstance(detail_raw, str) else (detail_raw or {})
            results.append({
                'check_key': row['check_key'],
                'status': row.get('status', 'unknown'),
                'message': row.get('message', ''),
                'detail': detail,
            })
        if not results:
            return jsonify({'success': False, 'error': _t('No check results for this run')}), 404
        from .ai_fixer import AIFixer
        fixer = AIFixer()
        plan = fixer.analyze({
            'run_id': latest['id'],
            'run_type': latest.get('run_type', 'unknown'),
            'results': results,
        })
        return jsonify({
            'success': True,
            'data': {
                'ok': not plan.get('_error'),
                'summary': plan.get('summary', ''),
                'error': plan.get('_error', ''),
                'health_score': plan.get('health_score'),
                'critical_issues': plan.get('critical_issues', []),
                'items': plan.get('items', plan.get('root_causes', [])),
                'raw_plan': plan,
            }
        })

    # Fetch detail from DB if not provided
    check_status = 'unknown'
    check_message = ''
    if not detail_override:
        with m.get_db() as conn:
            if run_id:
                row = conn.execute(
                    'SELECT * FROM check_history WHERE run_id=%s AND check_key=%s',
                    (run_id, check_key)
                ).fetchone()
            else:
                row = conn.execute(
                    'SELECT * FROM check_history WHERE check_key=%s ORDER BY id DESC LIMIT 1',
                    (check_key,)
                ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _t('Check result not found')}), 404
        detail_raw = row['detail'] or '{}'
        detail_override = json.loads(detail_raw) if isinstance(detail_raw, str) else (detail_raw or {})
        check_status = row.get('status', 'unknown')
        check_message = row.get('message', '')

    # Build input for LLM
    check_results = {
        'check_key': check_key or 'manual',
        'status': check_status or 'unknown',
        'message': check_message or '',
        'detail': detail_override or {},
    }

    from .ai_fixer import AIFixer
    fixer = AIFixer()
    plan = fixer.analyze(check_results)

    return jsonify({
        'success': True,
        'data': {
            'ok': not plan.get('_error'),
            'summary': plan.get('summary', ''),
            'error': plan.get('_error', ''),
            'items': plan.get('items', []),
            'raw_plan': plan,
        }
    })


@health_bp.route('/api/ai-fix', methods=['POST'])
def api_ai_fix():
    """
    Execute AI-suggested fixes for a health check result.

    Request body:
    {
        "run_id": 123,
        "check_key": "internal_links",
        "items": [          // Optional — fix items from a previous ai-analyze response
            {"action": "update_url", "params": {...}, "reason": "..."}
        ],
        "confirm": true     // Must be true to execute
    }
    """
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    data = request.json or {}
    confirm = data.get('confirm', False)
    run_id = data.get('run_id')
    check_key = data.get('check_key', '')
    items = data.get('items')

    if not confirm:
        return jsonify({'success': False, 'error': 'Please confirm execution (confirm: true)'}), 400

    if not items:
        return jsonify({'success': False, 'error': 'No fix items provided'}), 400

    from .ai_fixer import AIFixer
    from .checkers import FixSuggestion, ALL_FIX_ACTIONS

    fixer = AIFixer()
    suggestions = []
    for item in items:
        action = item.get('action', '')
        if action not in ALL_FIX_ACTIONS:
            continue
        suggestions.append(FixSuggestion(
            action=action,
            reason=item.get('reason', ''),
            params=item.get('params', {}),
            record_type=check_key,
        ))

    if not suggestions:
        return jsonify({'success': False, 'error': 'No valid fix actions found'}), 400

    # Use broad search_path so fix SQL can access tables in both health and public schemas
    from plugins._base.db import get_raw_connection, PgConnection
    raw = get_raw_connection()
    raw.autocommit = False
    try:
        conn = PgConnection(raw)
        conn.execute("SET search_path TO health, public")
        result = fixer.execute_fix(conn, suggestions)
        conn.commit()
    finally:
        raw.close()

    return jsonify({
        'success': True,
        'data': {
            'applied': result['applied'],
            'total': result['total'],
            'errors': result['errors'],
            'run_id': run_id,
            'check_key': check_key,
        },
        'message': _t('Applied {applied}/{total} fixes').format(
            applied=result['applied'], total=result['total']
        )
    })


# ─── Fix Audit & Rollback API ──────────────────────────────────────────────

@health_bp.route('/api/fix/audit')
def api_fix_audit():
    """Query fix audit log with optional filters."""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    limit = min(request.args.get('limit', 50, type=int), 200)
    check_key = request.args.get('check_key', '')
    action = request.args.get('action', '')
    status = request.args.get('status', '')

    with m.get_db() as conn:
        where = ''
        params = []
        if check_key:
            where += ' AND check_key=?' if where else 'WHERE check_key=?'
            params.append(check_key)
        if action:
            where += ' AND action=?' if where else 'WHERE action=?'
            params.append(action)
        if status:
            where += ' AND status=?' if where else 'WHERE status=?'
            params.append(status)

        total = conn.execute(
            f'SELECT COUNT(*) as c FROM fix_audit_log {where}', params
        ).fetchone()['c']

        rows = conn.execute(
            f'SELECT * FROM fix_audit_log {where} ORDER BY created_at DESC LIMIT ?',
            params + [limit]
        ).fetchall()

        import json as _json
        results = []
        for r in rows:
            d = dict(r)
            for col in ('params_json', 'undo_params_json'):
                try:
                    d[col] = _json.loads(d.get(col, '{}'))
                except (_json.JSONDecodeError, TypeError):
                    d[col] = {}
            results.append(d)

    return jsonify({
        'success': True,
        'data': {
            'total': total,
            'audit_logs': results,
        }
    })


@health_bp.route('/api/fix/rollback/<int:audit_id>', methods=['POST'])
def api_fix_rollback(audit_id):
    """Rollback a previously applied fix."""
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    with m.get_db() as conn:
        row = conn.execute(
            'SELECT * FROM fix_audit_log WHERE id=%s AND status=%s',
            (audit_id, 'applied')
        ).fetchone()

        if not row:
            return jsonify({'success': False, 'error': 'Audit record not found or already rolled back'}), 404

        row = dict(row)
        import json as _json
        try:
            undo = _json.loads(row['undo_params_json'])
        except (_json.JSONDecodeError, TypeError):
            undo = {}

        action = row['action']
        success = False

        try:
            # SQL 标识符白名单：undo 的 table/field 来自 LLM 构造，拼接进 SQL 前必须校验
            import re as _re
            if undo.get('table') and not _re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', str(undo['table'])):
                return jsonify({'success': False, 'error': 'Invalid table identifier'}), 400
            if undo.get('field') and not _re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', str(undo['field'])):
                return jsonify({'success': False, 'error': 'Invalid field identifier'}), 400

            if action == 'set_log_level' and undo.get('old_level'):
                conn.execute(
                    "INSERT INTO system_config (key, value) VALUES ('log_level', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (undo['old_level'],)
                )
                success = True

            elif action == 'update_url' and undo.get('table') and undo.get('old_value'):
                conn.execute(
                    f"UPDATE {undo['table']} SET {undo['field']}=%s WHERE id=%s",
                    (undo['old_value'], undo['record_id'])
                )
                success = True

            elif action == 'mark_disabled' and undo.get('table') and undo.get('record_id'):
                if 'old_is_enabled' in undo:
                    conn.execute(
                        f"UPDATE {undo['table']} SET is_enabled=%s WHERE id=%s",
                        (undo['old_is_enabled'], undo['record_id'])
                    )
                if 'old_is_active' in undo:
                    conn.execute(
                        f"UPDATE {undo['table']} SET is_active=%s WHERE id=%s",
                        (undo['old_is_active'], undo['record_id'])
                    )
                success = True

            elif action == 'mark_deleted' and undo.get('old_status'):
                conn.execute(
                    f"UPDATE {undo['table']} SET status=%s WHERE id=%s",
                    (undo['old_status'], undo['record_id'])
                )
                success = True

            elif action == 'clear_field' and undo.get('old_value'):
                conn.execute(
                    f"UPDATE {undo['table']} SET {undo['field']}=%s WHERE id=%s",
                    (undo['old_value'], undo['record_id'])
                )
                success = True

            elif action in ('clean_temp', 'restart_worker', 'flush_cdn'):
                return jsonify({
                    'success': False,
                    'error': f'Rollback not supported for {action}: {undo.get("note", "No undo data")}'
                }), 400

            if success:
                conn.execute(
                    'UPDATE fix_audit_log SET status=%s WHERE id=%s',
                    ('rolled_back', audit_id)
                )
                conn.commit()
                return jsonify({'success': True, 'message': f'Fix #{audit_id} rolled back'})

        except Exception as e:
            return jsonify({'success': False, 'error': f'Rollback failed: {e}'}), 500

    return jsonify({'success': False, 'error': 'No reversible data found'}), 400


# ─── Internal API: Called by Workflow Engine ─────────────────────────────────

@health_bp.route('/api/internal/run', methods=['POST'])
def api_internal_run():
    """
    Internal API called by Workflow/Cron (no admin login required, uses secret token for authentication)
    Request header must include X-Health-Secret
    """
    secret = request.headers.get('X-Health-Secret', '')
    # 审计 D5：HEALTH_SECRET 未配置时 fail-closed（拒绝），不再使用源码公开默认值
    expected = os.environ.get('HEALTH_SECRET', '')
    if not expected or secret != expected:
        return jsonify({'success': False, 'error': 'Forbidden'}), 403

    data = request.json or {}
    trigger_type = data.get('trigger_type', 'scheduled')
    check_keys = data.get('checks')
    trigger_info = data.get('trigger_info', 'internal')

    run_id = run_health_check(trigger_type=trigger_type, trigger_info=trigger_info,
                              check_keys=check_keys)

    return jsonify({
        'success': True,
        'data': {'run_id': run_id, 'message': _t('Health check completed (ID: {id})').format(id=run_id)}
    })


# ═══════════════════════════════════════════════════════════════════════════
# Health Guardian Log API
# ═══════════════════════════════════════════════════════════════════════════

@health_bp.route('/api/guardian-log')
def api_guardian_log():
    """
    Read Health Guardian / VeroGuard log file.
    优先读取 VeroGuard 日志，回退到旧版 health-guardian 日志。
    """
    admin = _require_admin()
    if not admin:
        return jsonify({'success': False, 'error': _t('Unauthorized')}), 401

    log_files = [
        os.environ.get('VEROGUARD_LOG_FILE', '/var/log/verorun-guardian.log'),
        os.environ.get('GUARDIAN_LOG_FILE', '/var/log/health-guardian.log'),
    ]
    lines = min(request.args.get('lines', 50, type=int), 200)

    for log_file in log_files:
        if os.path.exists(log_file):
            try:
                with open(log_file, 'r') as f:
                    content = f.read()
                all_lines = content.splitlines()
                total = len(all_lines)
                tail = all_lines[-lines:] if total > lines else all_lines
                return jsonify({'success': True, 'data': tail, 'total': total, 'source': log_file})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': True, 'data': [], 'total': 0,
                    'message': _t('No guardian log file found')})
