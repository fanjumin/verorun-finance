#!/usr/bin/env python3
"""
Subscription Plugin — 定时任务
================================
通过 APScheduler 注册定时任务（__init__.py register_jobs() → SUBSCRIPTION_JOBS）:
  - 每日扫描到期订阅并自动续费（代扣）
  - 每日 dunning 重试 + 宽限期过期锁定
  - 每日清理 90 天前的 pending 订单
"""
from datetime import datetime, timedelta

from .services import get_subscription_service

# Dunning 重试计划（对齐 A: auth-center/routes/subscription/renewal.py）
DUNNING_DAYS = [1, 3, 7]
GRACE_DAYS = 7


def run_renewal_scan():
    """每日扫描：查找今天到期的活跃自动续费订阅并执行代扣"""
    svc = get_subscription_service()
    today = datetime.now().date().isoformat()

    with svc._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_subscriptions "
            "WHERE status='active' AND auto_renew=1 "
            "  AND date(period_end::timestamp) <= %s",
            (today,)
        ).fetchall()

    if not rows:
        return

    print(f'[Subscription/Job] {len(rows)} subscription(s) due for renewal')
    for row in rows:
        rd = dict(row)
        success, msg = svc.renew_subscription(rd['user_id'], rd['item_key'])
        if success:
            print(f'[Subscription/Job] Auto-renewed: user={rd["user_id"]}, item={rd["item_key"]}')
        else:
            print(f'[Subscription/Job] Auto-renew failed: user={rd["user_id"]}, item={rd["item_key"]}, msg={msg}')


def run_dunning_scan():
    """每日 dunning：宽限期内重试扣款；超宽限期 → 过期锁定"""
    svc = get_subscription_service()
    svc.run_dunning_scan()


def check_expired_subscriptions():
    """每天检查到期订阅（非自动续费 → 标记 expired）"""
    svc = get_subscription_service()
    expired = svc.check_expired()
    if expired:
        print(f'[Subscription/Job] Found {len(expired)} expired subscriptions')


def cleanup_old_orders():
    """清理 90 天前的过期订单"""
    svc = get_subscription_service()
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()

    with svc._get_conn() as conn:
        conn.execute(
            "UPDATE sub_orders SET status='expired', updated_at=NOW() "
            "WHERE status='pending' AND created_at::timestamp < %s",
            (cutoff,)
        )
        conn.commit()


# ── 注册到 APScheduler ───────────────────────────────────────────────────

# M-04：按标准 §3.2 register_jobs() 配置格式定义
#   job_id / func / trigger / kwargs / priority / max_retries
SUBSCRIPTION_JOBS = [
    {
        'job_id': 'subscription_renewal_scan',
        'func': run_renewal_scan,
        'trigger': 'cron',
        'kwargs': {'hour': 2, 'minute': 0},
        'priority': 'normal',
        'max_retries': 2,
        'description': 'Daily auto-renew charge for subscriptions due today',
    },
    {
        'job_id': 'subscription_dunning_scan',
        'func': run_dunning_scan,
        'trigger': 'cron',
        'kwargs': {'hour': 3, 'minute': 0},
        'priority': 'normal',
        'max_retries': 2,
        'description': 'Daily dunning retry + grace period expiry lock',
    },
    {
        'job_id': 'subscription_check_expired',
        'func': check_expired_subscriptions,
        'trigger': 'cron',
        'kwargs': {'hour': 2, 'minute': 30},
        'priority': 'low',
        'max_retries': 2,
        'description': 'Daily check for expired subscriptions (non auto-renew)',
    },
    {
        'job_id': 'subscription_cleanup_orders',
        'func': cleanup_old_orders,
        'trigger': 'cron',
        'kwargs': {'hour': 3, 'minute': 30},
        'priority': 'low',
        'max_retries': 2,
        'description': 'Cleanup pending orders older than 90 days',
    },
]
