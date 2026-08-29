#!/usr/bin/env python3
"""Notification Service — centralized notification creation and event-driven sending."""

import json
import re
import time

import psycopg2.extras
from i18n import _
from models.database import get_db

# ── Rate limiting ──
_rate_limit_cache = {}  # { user_id: [timestamps...] }
RATE_LIMIT_PER_MIN = 10


def _check_rate_limit(user_id):
    """Rate limit: max RATE_LIMIT_PER_MIN notifications per user per minute."""
    now = time.time()
    timestamps = _rate_limit_cache.get(user_id, [])
    # Keep only timestamps within the last 60s
    timestamps = [t for t in timestamps if now - t < 60]
    if len(timestamps) >= RATE_LIMIT_PER_MIN:
        return False
    timestamps.append(now)
    _rate_limit_cache[user_id] = timestamps
    return True


def _substitute_vars(template, context_vars):
    """Replace {var} placeholders with values from context_vars dict."""
    def replacer(m):
        key = m.group(1)
        return str(context_vars.get(key, m.group(0)))
    return re.sub(r'\{(\w+)\}', replacer, template)


# ══════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════

def create_notification(user_id, ntype, title, content, link_url=None, extra_data=None):
    """Create a single notification for a user. Returns the notification ID or None if rate limited."""
    if not _check_rate_limit(user_id):
        return None
    if extra_data is None:
        extra_data = {}
    try:
        with get_db() as conn:
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            cur = conn.execute(
                'INSERT INTO user_notifications (user_id, type, title, content, link_url, extra_data) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
                (user_id, ntype, title, content, link_url or '', json.dumps(extra_data, ensure_ascii=False))
            )
            row = cur.fetchone()
            return row['id'] if row else None
    except Exception:
        return None


def send_notification_by_event(event_type, user_id, context_vars=None):
    """
    Event-driven notification: looks up a matching template, substitutes variables,
    creates the notification. Returns dict with notification_id or error.
    
    context_vars example: {'username': '张三', 'reward_name': '新人优惠券', 'friend_name': '李四'}
    """
    if context_vars is None:
        context_vars = {}
    with get_db() as conn:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        # Find the active template for this event_type
        template = conn.execute(
            'SELECT * FROM notification_templates WHERE event_type=%s AND is_active=1',
            (event_type,)
        ).fetchone()
        if not template:
            return {'success': False, 'error': _('No active template for event: {event_type}', event_type=event_type)}

        # Translate template (locale-aware, falls back to original text) then substitute variables
        title = _substitute_vars(_(template['title_template']), context_vars)
        content = _substitute_vars(_(template['content_template']), context_vars)
        link_url = ''
        if template.get('link_url_template'):
            link_url = _substitute_vars(template['link_url_template'], context_vars)

        # Create notification
        nid = create_notification(
            user_id=user_id,
            ntype=template.get('type', 'system'),
            title=title,
            content=content,
            link_url=link_url,
            extra_data={'event_type': event_type}
        )
        if nid is None:
            return {'success': False, 'error': _('Rate limited or creation failed')}

        # Log the send
        conn.execute(
            'INSERT INTO notification_logs (template_id, user_id, event_type, notification_id, result) VALUES (%s,%s,%s,%s,%s)',
            (template['id'], user_id, event_type, nid, 'success')
        )

        return {'success': True, 'notification_id': nid}


def get_unread_count(user_id):
    """Return the number of unread notifications for a user."""
    with get_db() as conn:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        row = conn.execute(
            'SELECT COUNT(*) as c FROM user_notifications WHERE user_id=%s AND is_read=0',
            (user_id,)
        ).fetchone()
        return row['c'] if row else 0


def mark_read(user_id, nid=None):
    """Mark a notification (or all) as read. Updates read_at timestamp."""
    try:
        with get_db() as conn:
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            if nid:
                conn.execute(
                    "UPDATE user_notifications SET is_read=1, read_at=NOW() WHERE user_id=%s AND id=%s",
                    (user_id, nid)
                )
            else:
                conn.execute(
                    "UPDATE user_notifications SET is_read=1, read_at=NOW() WHERE user_id=%s",
                    (user_id,)
                )
        return True
    except Exception:
        return False


def send_to_all_users(ntype, title, content, link_url=None, limit=1000):
    """
    Send a notification to all active users.
    Returns count of notifications sent.
    """
    with get_db() as conn:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        users = conn.execute(
            'SELECT id FROM users WHERE active=1 ORDER BY id LIMIT %s',
            (limit,)
        ).fetchall()
        sent = 0
        for u in users:
            nid = create_notification(u['id'], ntype, title, content, link_url)
            if nid:
                sent += 1
        return sent
