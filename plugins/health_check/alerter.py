#!/usr/bin/env python3
"""
Health Check — Alert Module (v2.0)
===================================
P0-P3 四级告警 + 聚合窗口 + 冷却抑制 + 静默窗口 + 自动升级

Alert level mapping:
  critical + consecutive >= 2  → P0 (Critical: 服务完全不可用)
  critical + consecutive = 1   → P1 (Major: 核心功能受损)
  warning  + consecutive >= 3  → P2 (Minor: 非核心异常)
  warning  + consecutive 1-2   → P3 (Info: 预警通知)
  info     + consecutive >= 5  → P3 (Info: 预警通知)

Notification channels:
  P0: email + internal + webhook (全部)
  P1: email + internal
  P2: email + internal
  P3: internal only
"""

import os, sys, json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center'))
sys.path.insert(0, os.path.join(BASE_DIR, '..'))

from .models import get_db

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('health_check')
except ImportError:
    import logging
    _logger = logging.getLogger('health_check')


# ─── Alert level helpers ──────────────────────────────────────────────────

def _determine_alert_level(check_status: str, consecutive_fails: int) -> Optional[str]:
    """Map check status + consecutive failures to P0-P3 alert level.
    
    Returns None if threshold not met (no alert should be sent).
    """
    if check_status == 'error':
        if consecutive_fails >= 2:
            return 'P0'
        return 'P1'
    elif check_status == 'warning':
        if consecutive_fails >= 3:
            return 'P2'
        return 'P3'
    elif check_status == 'info':
        if consecutive_fails >= 5:
            return 'P3'
        return None
    return None


def _level_label(level: str) -> str:
    return {'P0': 'CRITICAL', 'P1': 'MAJOR', 'P2': 'MINOR', 'P3': 'INFO'}.get(level, 'UNKNOWN')


def _level_emoji(level: str) -> str:
    return {'P0': '🔴', 'P1': '🟠', 'P2': '🟡', 'P3': '🔵'}.get(level, '⚪')


def _level_notify_methods(level: str) -> List[str]:
    """Return enabled notification methods for a given alert level."""
    return {
        'P0': ['email', 'internal', 'webhook'],
        'P1': ['email', 'internal'],
        'P2': ['email', 'internal'],
        'P3': ['internal'],
    }.get(level, ['internal'])


# ─── Silence / Cooldown / Aggregation checks ─────────────────────────────

def _is_in_silence_window(conn, check_key: str) -> bool:
    """Check if this check_key is currently silenced."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    row = conn.execute(
        "SELECT id FROM alert_silences WHERE "
        "(check_key='*' OR check_key=%s) "
        "AND starts_at <= %s AND ends_at >= %s LIMIT 1",
        (check_key, now, now)
    ).fetchone()
    return row is not None


def _is_in_cooldown(conn, check_key: str, cooldown_minutes: int) -> bool:
    """Check if same check_key already alerted within cooldown window."""
    row = conn.execute(
        "SELECT id FROM alert_history WHERE check_key=%s "
        "AND created_at >= NOW() + %s::INTERVAL LIMIT 1",
        (check_key, f'-{cooldown_minutes} minutes')
    ).fetchone()
    return row is not None


def _check_aggregation(conn, check_key: str, window_seconds: int) -> bool:
    """Check if failures are sustained across the aggregation window.
    
    Returns True if all checks in the window failed (condition is sustained).
    If window is 0, returns True immediately (instant alert).
    """
    if window_seconds <= 0:
        return True

    since = (datetime.now() - timedelta(seconds=window_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    recent = conn.execute(
        "SELECT status FROM check_history WHERE check_key=%s AND checked_at >= %s "
        "ORDER BY checked_at DESC",
        (check_key, since)
    ).fetchall()

    if not recent:
        return False  # No data in window, don't alert

    # All checks in the window must have failed
    return all(r['status'] in ('warning', 'error') for r in recent)


# ─── Main alert logic ─────────────────────────────────────────────────────

def evaluate_and_alert(run_id: int, check_results: List[dict]):
    """
    Evaluate check results with P0-P3 alert levels, aggregation window,
    cooldown suppression, and silence windows.
    """
    with get_db() as conn:
        rules = conn.execute(
            'SELECT * FROM alert_config WHERE is_active=1'
        ).fetchall()

        for rule in rules:
            rule = dict(rule)
            check_key_filter = rule.get('check_key', '*')

            # Find matching failed results
            relevant_results = [
                r for r in check_results
                if (check_key_filter == '*' or r.get('check_key') == check_key_filter)
                and r.get('status') in ('warning', 'error')
            ]

            if not relevant_results:
                continue

            for result in relevant_results:
                key = result.get('check_key', '')
                status = result.get('status', '')
                name = result.get('check_name', key)

                # 1. Count consecutive failures
                consecutive_fails = count_consecutive_fails(conn, key)
                rule_consecutive = rule.get('consecutive', 1)
                if consecutive_fails < rule_consecutive:
                    continue

                # 2. Determine alert level
                alert_level = _determine_alert_level(status, consecutive_fails)
                if not alert_level:
                    continue

                # 3. Check silence window
                if _is_in_silence_window(conn, key):
                    _logger.info('Silenced: %s (level=%s)', key, alert_level)
                    continue

                # 4. Check cooldown
                cooldown = rule.get('cooldown_minutes', 60)
                if _is_in_cooldown(conn, key, cooldown):
                    _logger.info('Cooldown: %s (level=%s)', key, alert_level)
                    continue

                # 5. Check aggregation window
                agg_window = rule.get('aggregation_window', 300)
                if not _check_aggregation(conn, key, agg_window):
                    _logger.info('Aggregation not met: %s (window=%ss)', key, agg_window)
                    continue

                # 6. Determine notification methods for this level
                notify_methods = _level_notify_methods(alert_level)
                notify_method_str = ','.join(notify_methods)

                # 7. Build message
                level_label = _level_label(alert_level)
                emoji = _level_emoji(alert_level)
                message = (
                    f'[{emoji} {level_label}] {name}\n'
                    f'Status: {status} | Consecutive fails: {consecutive_fails}\n'
                    f'{result.get("message", "")}'
                )

                # 8. Record in alert_history
                conn.execute(
                    'INSERT INTO alert_history '
                    '(alert_config_id, check_key, check_name, run_id, status, alert_level, message, notify_method) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                    (rule['id'], key, name, run_id, status, alert_level, message, notify_method_str)
                )
                conn.commit()

                _logger.warning('ALERT [%s] %s: %s (consec=%d)', alert_level, key, status, consecutive_fails)

                # 9. Send notifications (filtered by alert level, not rule config)
                for method in notify_methods:
                    if method == 'email':
                        _send_email_alert(message, alert_level)
                    elif method == 'internal':
                        _send_internal_message(message, alert_level)
                    elif method == 'webhook':
                        _send_webhook(message, rule.get('webhook_url', ''))


def count_consecutive_fails(conn, check_key: str) -> int:
    """Count consecutive failures for a given check key."""
    recent = conn.execute(
        "SELECT status FROM check_history WHERE check_key=%s "
        "ORDER BY checked_at DESC LIMIT 10",
        (check_key,)
    ).fetchall()

    count = 0
    for r in recent:
        if r['status'] in ('warning', 'error'):
            count += 1
        else:
            break
    return count


def send_notification(method: str, message: str, rule: dict):
    """(Unused by new engine — kept for API backwards compatibility)"""
    _send_email_alert(message, 'P2')
    _send_internal_message(message, 'P2')
    if rule.get('webhook_url'):
        _send_webhook(message, rule.get('webhook_url', ''))


# ─── Notification senders ─────────────────────────────────────────────────

def _send_email_alert(message: str, alert_level: str = 'P2'):
    """Send alert via email."""
    try:
        from plugins.email.services import send_email
        level_label = _level_label(alert_level)
        emoji = _level_emoji(alert_level)
        color = {'P0': '#f85149', 'P1': '#f0883e', 'P2': '#fbbf24', 'P3': '#60a5fa'}.get(alert_level, '#8b949e')
        with get_db() as conn:
            admins = conn.execute(
                "SELECT email FROM users WHERE is_admin=1 AND email IS NOT NULL AND email!=''"
            ).fetchall()
        for admin in admins:
            html_body = (
                f'<div style="background:#0d1117;color:#c9d1d9;padding:20px;font-family:sans-serif">'
                f'<h2 style="color:{color}">{emoji} [{alert_level}] {level_label}</h2>'
                f'<p style="white-space:pre-wrap">{message}</p>'
                f'<p style="color:#8b949e;font-size:12px">Sent at: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>'
                f'<p><a href="https://agent.your-domain.com/admin/health" style="color:#58a6ff">View in Admin Panel</a></p>'
                f'</div>'
            )
            send_email(
                to_addr=admin['email'],
                subject=f'{emoji} [{alert_level}] {level_label} — System Health Alert',
                body_text=message,
                body_html=html_body,
            )
    except Exception as e:
        _logger.error('Failed to send email: %s', e)


def _send_internal_message(message: str, alert_level: str = 'P2'):
    """Send internal notification."""
    try:
        level_label = _level_label(alert_level)
        emoji = _level_emoji(alert_level)
        title = f'{emoji} [{alert_level}] {level_label} — System Health Alert'
        with get_db() as conn:
            admins = conn.execute("SELECT id FROM users WHERE is_admin=1").fetchall()
            for admin in admins:
                conn.execute(
                    "INSERT INTO admin_notifications (user_id, title, content, is_read, created_at) "
                    "VALUES (%s, %s, %s, 0, NOW())",
                    (admin['id'], title, message)
                )
            conn.commit()
    except Exception as e:
        _logger.error('Failed to send internal message: %s', e)


def _send_webhook(message: str, webhook_url: str):
    """Send alert via webhook."""
    if not webhook_url:
        return
    # Block private/internal IPs (§11.3)
    try:
        from .checkers import _extract_host_from_url, _is_private_host
        host = _extract_host_from_url(webhook_url)
        if host and _is_private_host(host):
            _logger.warning('Webhook blocked (private IP: %s)', host)
            return
    except ImportError:
        pass  # Gracefully degrade if checkers module not loaded
    try:
        import urllib.request
        data = json.dumps({
            'event': 'health_alert',
            'message': message,
            'timestamp': datetime.now().isoformat(),
            'source': 'health-monitor',
        }).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={'Content-Type': 'application/json'},
                                     method='POST')
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        _logger.error('Failed to send webhook: %s', e)
