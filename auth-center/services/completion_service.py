#!/usr/bin/env python3
"""
Profile Completion Service — calculates completion % and checks milestone rewards.
"""

import json
import os
import sys

import psycopg2.extras
from i18n import _
from models.database import get_db


def _has_user_interests(user_id):
    """Check if user has at least one interest selected in user_interests table."""
    try:
        with get_db() as conn:
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            row = conn.execute(
                'SELECT COUNT(*) as cnt FROM user_interests WHERE user_id=%s', (user_id,)
            ).fetchone()
            return (row['cnt'] if row else 0) > 0
    except Exception:
        return False


# ── Field definition ──
# Each entry: (field_key, display_name, check_fn)
# check_fn receives a dict {user, profile} and returns bool
FIELD_DEFS = [
    ('display_name',     'Display name',  lambda u, p: bool((u.get('display_name') or '').strip())),
    ('avatar_url',       'Avatar',        lambda u, p: bool((u.get('avatar_url') or '').strip())),
    ('phone_verified',   'Phone verified',lambda u, p: u.get('phone_verified', 0) == 1),
    ('gender',           'Gender',        lambda u, p: bool(p and (p.get('gender') or '').strip())),
    ('birth_date',       'Date of birth', lambda u, p: bool(p and (p.get('birth_date') or '').strip())),
    ('profile_detail',   'Profile details', lambda u, p: bool(p and (
        p.get('industry_id') or p.get('career_id') or
        (p.get('interests') or '[]') not in ('[]', '') or
        bool((p.get('bio') or '').strip())
    ))),
    ('interests_set',    'Interest tags', lambda u, p: _has_user_interests(u['id'])),
    ('email_verified',   'Email verified',lambda u, p: u.get('email_verified', 0) == 1),
]


def calc_completion(user_id):
    """
    Calculate profile completion percentage and detailed breakdown.
    Returns dict: { completion: int, items: [{key, name, done}], total_fields: int, filled: int }
    """
    with get_db() as conn:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        user = conn.execute('SELECT * FROM users WHERE id=%s', (user_id,)).fetchone()
        if not user:
            return {'completion': 0, 'items': [], 'total_fields': len(FIELD_DEFS), 'filled': 0}
        prof = conn.execute('SELECT * FROM user_profiles WHERE user_id=%s', (user_id,)).fetchone()
        prof = dict(prof) if prof else {}

        items = []
        filled = 0
        for key, name, check_fn in FIELD_DEFS:
            done = check_fn(user, prof)
            items.append({'key': key, 'name': _(name), 'done': done})
            if done:
                filled += 1

        total = len(FIELD_DEFS)
        completion = (filled * 100) // total if total > 0 else 0

        return {
            'completion': completion,
            'items': items,
            'total_fields': total,
            'filled': filled,
        }


def save_completion(user_id):
    """Calculate and persist completion_percentage + timestamp."""
    result = calc_completion(user_id)
    pct = result['completion']
    with get_db() as conn:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        conn.execute(
            "UPDATE users SET completion_percentage=%s, completion_last_updated=NOW() WHERE id=%s",
            (pct, user_id)
        )
    return result


def check_milestone_rewards(user_id):
    """
    After profile update, check all unclaimed reward rules and issue rewards.
    Returns list of issued rewards.
    """
    with get_db() as conn:
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        issued = []
        try:
            # Get current user state
            user = conn.execute('SELECT * FROM users WHERE id=%s', (user_id,)).fetchone()
            if not user:
                return issued
            prof = conn.execute('SELECT * FROM user_profiles WHERE user_id=%s', (user_id,)).fetchone()
            prof = dict(prof) if prof else {}

            # Get completion
            comp_result = calc_completion(user_id)
            completion_pct = comp_result['completion']

            # Load all active rules
            rules = conn.execute('SELECT * FROM reward_rules WHERE is_active=1 ORDER BY sort_order').fetchall()

            for rule in rules:
                # Check if already claimed
                claimed = conn.execute(
                    'SELECT id FROM reward_claims WHERE user_id=%s AND rule_id=%s',
                    (user_id, rule['id'])
                ).fetchone()
                if claimed:
                    continue

                # Evaluate condition
                match = False
                cond_key = rule['condition_key']
                cond_val = rule['condition_value']

                if cond_key == 'completion_percentage':
                    threshold = int(cond_val)
                    if completion_pct >= threshold:
                        match = True
                elif cond_key == 'phone_verified':
                    if user.get('phone_verified', 0) == 1:
                        match = True
                elif cond_key == 'email_verified':
                    if user.get('email_verified', 0) == 1:
                        match = True
                elif cond_key == 'avatar_set':
                    if bool(user.get('avatar_url', '').strip()):
                        match = True
                elif cond_key == 'has_profile':
                    if prof and any([
                        prof.get('gender'), prof.get('birth_date'),
                        prof.get('industry_id'), prof.get('career_id'),
                        prof.get('interests', '[]') not in ('[]', ''),
                        prof.get('bio', '').strip()
                    ]):
                        match = True
                else:
                    # Generic field check on user or profile
                    if cond_key in user:
                        if str(user[cond_key]) == cond_val:
                            match = True
                    elif prof and cond_key in prof:
                        if str(prof[cond_key]) == cond_val:
                            match = True

                if not match:
                    continue

                # Issue reward
                coupon_id = None
                if rule['reward_type'] == 'coupon' and rule['reward_id']:
                    # 走插件引擎分发优惠券
                    try:
                        from shared.plugin_access import get_attr
                        get_engine = get_attr('plugins.coupons', 'get_engine',
                                              feature='completion_coupon')
                        engine = get_engine() if get_engine else None
                        if engine:
                            engine.distribute(rule['reward_id'], [user_id])
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            '[completion_service] 优惠券发放失败 (rule=%s, user=%s): %s',
                            rule.get('id'), user_id, e)

                # Record claim
                conn.execute(
                    'INSERT INTO reward_claims (user_id, rule_id, coupon_id) VALUES (%s,%s,%s)',
                    (user_id, rule['id'], coupon_id)
                )
                issued.append({
                    'rule_id': rule['id'],
                    'rule_name': rule['name'],
                    'reward_type': rule['reward_type'],
                    'reward_name': rule.get('reward_name', ''),
                    'coupon_id': coupon_id,
                })

            if issued:
                # Send notifications for each issued reward
                try:
                    from services.notification_service import send_notification_by_event
                    for item in issued:
                        send_notification_by_event(
                            'reward.issued',
                            user_id,
                            {'reward_name': item['reward_name'] or item['rule_name']}
                        )
                except ImportError:
                    pass  # notification service not available, silently skip

        except Exception as e:
            print(f'[RewardChecker] Error for user {user_id}: {e}', file=sys.stderr)
            conn.rollback()
            return []

    return issued


def refresh_and_check(user_id):
    """Convenience: save completion + check rewards. Returns completion result + issued rewards."""
    result = save_completion(user_id)
    issued = check_milestone_rewards(user_id)
    result['rewards_issued'] = issued
    return result
