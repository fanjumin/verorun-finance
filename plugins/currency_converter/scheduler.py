#!/usr/bin/env python3
"""
Currency Converter Scheduler — 定时同步汇率
=============================================
由 APScheduler 驱动，每小时从外部 API 同步最新汇率。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def sync_exchange_rates():
    """APScheduler 定时任务：同步汇率（每小时）"""
    try:
        from .services import sync_rates
        count = asyncio.run(sync_rates())
        if count > 0:
            logger.info(f'[CurrencyConverter][Scheduler] Synced {count} rates')
        else:
            logger.warning('[CurrencyConverter][Scheduler] Sync returned 0 rates, using cache')
    except Exception as e:
        logger.error(f'[CurrencyConverter][Scheduler] Sync failed: {e}')
