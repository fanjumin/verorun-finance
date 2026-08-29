#!/usr/bin/env python3
"""
模块策略定时任务 — Phase 2
==========================
每日凌晨扫描，处理模块试用到期和退款窗口到期。
由 orchestrator APScheduler 或 cron 调用。
"""
import logging

logger = logging.getLogger(__name__)


def run_module_policy_scan():
    """
    每日扫描任务：
    1. 试用到期 → 执行 post_trial_action（lock/pause/pay_per_use）
    2. 退款窗口到期 → paying → active

    由 APScheduler 调度：每天凌晨 2:00 执行
    """
    try:
        from services.module_policy import get_policy_engine
        engine = get_policy_engine()
        engine.daily_scan()
        logger.info("[ModuleScheduler] Daily scan completed")
    except Exception as e:
        logger.error(f"[ModuleScheduler] Daily scan failed: {e}")


def register_module_scheduler(scheduler):
    """
    注册模块策略每日扫描到 APScheduler。

    用法（在 orchestrator/scheduler.py 或 admin/app.py 中）：
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'auth-center'))
        from services.module_scheduler import register_module_scheduler
        register_module_scheduler(scheduler_instance)
    """
    try:
        scheduler.add_job(
            run_module_policy_scan,
            'cron',
            hour=2,
            minute=0,
            id='module_policy_daily_scan',
            name='模块策略每日扫描',
            replace_existing=True,
        )
        logger.info("[ModuleScheduler] Registered daily scan at 02:00")
    except Exception as e:
        logger.error(f"[ModuleScheduler] Registration failed: {e}")
