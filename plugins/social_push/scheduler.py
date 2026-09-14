#!/usr/bin/env python3
"""Social Push Plugin — 定时任务声明（P4：发布队列派发）

参照 im_gateway / risk_control 已落地模式，通过 register_jobs() 注册。
- 到期任务扫描派发（interval 60s，PG advisory lock 防多 worker 并发）
"""
import logging

from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('social_push')


def get_jobs(plugin_instance):
    """返回 APScheduler job dict 列表（插件管理器统一调度）"""
    return [
        {
            'id': 'social_push_due_scan',
            'func': dispatch_due_publishes,
            'trigger': 'interval',
            'seconds': 60,
        },
    ]


def dispatch_due_publishes():
    """扫描到期发布任务并执行（由 queue.dispatch_due 内部加 advisory lock）。"""
    try:
        from .services.queue import dispatch_due
        count = dispatch_due()
        if count:
            logger.info('[SocialPush] dispatched %d due publish task(s)', count)
        return count
    except Exception as e:
        logger.exception('[SocialPush] due publish dispatch failed: %s', e)
        return 0
