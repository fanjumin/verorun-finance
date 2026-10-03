# -*- coding: utf-8 -*-
"""晨报内容生成（报告 O6 闭环的最后一环）。

职责边界：本模块只做「生成 + 派发」，不做「投递」。投递由订阅方（email
插件）完成，故此处不得 import email —— 跨插件硬耦合会在 email 未启用时抛
ImportError，把整个定时任务带崩。

派发通道纪律（D10 教训，勿改）：
    email 侧用 get_event_handlers() 订阅 = hook registry 通道，
    故此处必须用 do_action() 发射；改用 get_event_bus().emit() 会静默失效。
"""
from __future__ import annotations

import logging
from datetime import date

_log = logging.getLogger(__name__)

# 会话级 advisory lock 键（照 alert_engine._SCAN_LOCK_KEY 范式）。
# gunicorn 多 worker 各持独立调度器，定时任务会被各 worker 各触发一次，
# 锁保证同一时刻仅一方真正派发，避免重复发信。
_MORNING_LOCK_KEY = "sa_morning_brief"

MORNING_BRIEF_HOOK = "stock.morning_brief.ready"


def build_morning_brief(trade_date: date | str | None = None) -> dict | None:
    """汇总指定交易日（默认上一交易日）的信号；无数据返回 None。

    名称来源优先级：sa_watchlist.alias > sa_symbol_master.name > symbol。
    sa_signal_log 只有代码不含名称，不补名称邮件里会全是 600519。
    """
    from .market_calendar import recent_trading_days
    from .models_sa import get_db, get_run_stats

    if trade_date is None:
        # 上一交易日；周一 08:30 自动取上周五
        trade_date = recent_trading_days()[1]
    trade_date = str(trade_date)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT s.symbol, "
            "       COALESCE(w.alias, m.name) AS name, "
            "       s.signal, s.confidence, s.kind "
            "  FROM sa_signal_log s "
            "  LEFT JOIN sa_watchlist w ON w.symbol = s.symbol "
            "  LEFT JOIN sa_symbol_master m ON m.symbol = s.symbol "
            " WHERE s.trade_date = ? "
            " ORDER BY s.confidence DESC NULLS LAST, s.symbol",
            (trade_date,)).fetchall()

    if not rows:
        return None

    groups: dict[str, list] = {}
    for r in rows:
        d = dict(r)
        sig = (d.get('signal') or '').lower() or 'n/a'
        conf = d.get('confidence')
        groups.setdefault(sig, []).append({
            'symbol': d['symbol'],
            'name': d.get('name') or d['symbol'],
            'signal': d.get('signal'),
            'confidence': float(conf) if conf is not None else None,
            'kind': d.get('kind'),
        })

    run = None
    try:
        run = get_run_stats(trade_date.replace('-', ''))
    except Exception as err:
        _log.warning("morning brief run stats failed: %s", err)

    return {
        'trade_date': trade_date,
        'total': len(rows),
        'groups': groups,
        'run': run,
    }


def dispatch_morning_brief() -> dict:
    """定时任务入口：生成晨报并以钩子派发（不直接发信）。

    返回 {skipped, reason} 或 {sent, trade_date, total}；任何异常都降级为
    skipped，不得影响同批其他定时任务。
    """
    from plugins._base.db import get_pooled_connection

    lock_conn = None
    held = False
    try:
        lock_conn = get_pooled_connection()
        row = lock_conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(?)) AS ok",
            (_MORNING_LOCK_KEY,)).fetchone()
        held = bool(row and row["ok"])
        if not held:
            return {"skipped": True, "reason": "lock-busy"}
    except Exception as err:
        _log.warning("morning brief lock failed: %s", err)
        return {"skipped": True, "reason": "lock-error"}

    try:
        try:
            from .models_sa import ensure_tables
            ensure_tables()
        except Exception as err:
            _log.warning("morning brief ensure_tables failed: %s", err)

        brief = build_morning_brief()
        if not brief:
            # 无数据不派发 —— 避免空邮件
            return {"skipped": True, "reason": "no-data"}

        from plugin_manager.hooks import get_hook_registry
        get_hook_registry().do_action(MORNING_BRIEF_HOOK, brief)
        return {"sent": True, "trade_date": brief['trade_date'], "total": brief['total']}
    except Exception as err:
        _log.warning("morning brief dispatch failed: %s", err)
        return {"skipped": True, "reason": "error"}
    finally:
        if held and lock_conn is not None:
            try:
                lock_conn.execute(
                    "SELECT pg_advisory_unlock(hashtext(?))", (_MORNING_LOCK_KEY,))
            except Exception:
                pass
        if lock_conn is not None:
            try:
                lock_conn.close()
            except Exception:
                pass
