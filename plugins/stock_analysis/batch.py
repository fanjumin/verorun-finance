"""batch.py — 交易日批量并发分析 + 落库（标准 v1.7 §11.2 定时任务节点）

三层幂等：
  1) 日历校验：非交易日直接跳过（离线 CSV + 周末规则双校验）
  2) 会话级 advisory lock：双 worker 并发时仅一方持有锁执行
  3) run_id（交易日）维度：sa_analysis_run 已 done 则跳过，结果/信号 INSERT 均 ON CONFLICT DO NOTHING

DB 不可用时降级为内存分析（不落库、不建表），保证批量链路不因存储故障中断。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from plugins._base.db import get_pooled_connection

try:
    from . import market_calendar as cal
    from . import models_sa as sa
except ImportError:  # 顶层脚本运行兜底
    from plugins.stock_analysis import market_calendar as cal
    from plugins.stock_analysis import models_sa as sa

_log = logging.getLogger("stock_analysis.batch")

_BATCH_KEY = "stock_analysis_batch"
_CHUNK = 50          # 每批并发上限
_MAX_WORKERS = 8     # 线程并发（gateway 信号量会再次约束各源实际并发）


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _analyze_one(symbol: str, kind: str, run_id: str, db_ready: bool):
    """单标的分析 + 落库；返回 (symbol, error_or_None)。"""
    try:
        from .stock_skill import StockAnalysisSkill
        skill = StockAnalysisSkill()
        result = skill.analyze(symbol, analysis_type=kind)
        if result.error:
            return symbol, result.error
        if db_ready:
            sa.upsert_result(run_id, symbol, kind, result.to_json())
        return symbol, None
    except Exception as err:
        _log.warning("batch analyze %s failed: %s", symbol, err)
        return symbol, str(err)


def latest_high_confidence_symbols(limit: int = 3, min_confidence: float = 0.7) -> list:
    """读取当日批量分析结果，返回置信度 >= min_confidence 的标的（高→低截断 limit 条）。

    接线点③配套：批量完成事件 → 自动深研联动取数。仅扫 sa_analysis_result（批量落库表），
    DB 不可用/无结果返回 []，调用方不受影响。
    """
    run_id = date.today().strftime("%Y%m%d")
    try:
        sa.ensure_tables()
        with sa.get_db() as conn:
            rows = conn.execute(
                "SELECT symbol FROM sa_analysis_result "
                "WHERE run_id = ? AND confidence IS NOT NULL AND confidence >= ? "
                "ORDER BY confidence DESC LIMIT ?",
                (run_id, min_confidence, limit)).fetchall()
        return [r["symbol"] for r in rows]
    except Exception as err:
        _log.warning("latest_high_confidence_symbols failed: %s", err)
        return []


def run_batch(kind: str = "technical", symbols=None):
    """执行一批批量分析。返回结果摘要 dict（skipped / 统计 / 状态）。"""
    today = date.today()

    # 幂等 ①：非交易日跳过（周末规则兜底）
    if not cal.is_trading_day(today):
        return {"skipped": "non-trading-day", "date": today.isoformat()}

    run_id = today.strftime("%Y%m%d")
    db_ready = False
    try:
        sa.ensure_tables()
        db_ready = True
    except Exception as err:
        _log.warning("DB not ready, batch runs in-memory only: %s", err)

    # 幂等 ②：会话级 advisory lock（双 worker 只放行一个）
    lock_conn = None
    lock_held = False
    if db_ready:
        try:
            lock_conn = get_pooled_connection()
            row = lock_conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(?)) AS ok", (_BATCH_KEY,)).fetchone()
            lock_held = bool(row and row["ok"])
            if lock_held and sa.run_exists(run_id):
                lock_conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (_BATCH_KEY,))
                lock_conn.close()
                # #SA-20260831-13：already-run 也返回完整统计契约（total/ok/failed/status），
                # 避免当日重复触发批量时响应缺字段（BATCH-02 判定失败）
                stats = sa.get_run_stats(run_id)
                if stats:
                    return dict(stats)
                return {"skipped": "already-run", "run_id": run_id}
        except Exception as err:
            # SAU-2：取锁异常由 fail-open（proceed without lock）改为 fail-closed。
            # 无锁并发跑批会撞 sa_analysis_result 的 UNIQUE 约束并落半截数据，
            # 宁可当日不跑、等下一次触发，也不产生不一致结果。
            _log.error("advisory lock unavailable, refusing to run batch: %s", err)
            if lock_conn:
                try:
                    lock_conn.close()
                except Exception:
                    pass
                lock_conn = None
            return {"skipped": "lock-unavailable", "run_id": run_id}

    # P3-6 修复：advisory lock 会话级锁异常路径泄漏——整个执行区间包 try/finally，
    # 任何异常（create_run / 并发 / finish_run）都确保 unlock + close，避免锁挂池连接。
    def _release_lock():
        if lock_held and lock_conn:
            try:
                lock_conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (_BATCH_KEY,))
            except Exception:
                pass
            try:
                lock_conn.close()
            except Exception:
                pass

    try:
        # 标的清单：入参优先，否则取 watchlist
        if not symbols:
            try:
                symbols = sa.watchlist_symbols()
            except Exception as err:
                _log.warning("watchlist unavailable, empty batch: %s", err)
                symbols = []
        symbols = [s for s in (symbols or []) if s and len(str(s)) <= 12]
        if not symbols:
            return {"skipped": "empty-watchlist", "run_id": run_id}

        if db_ready:
            sa.create_run(run_id, kind, len(symbols))

        # 幂等 ③：分批并发
        ok_n = failed_n = 0
        for chunk in _chunks(symbols, _CHUNK):
            with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
                for _sym, err in ex.map(
                        lambda s: _analyze_one(s, kind, run_id, db_ready), chunk):
                    if err:
                        failed_n += 1
                    else:
                        ok_n += 1

        llm_calls = ok_n if kind == "llm" else 0
        if db_ready:
            status = "done" if failed_n == 0 else "degraded"
            sa.finish_run(run_id, ok_n, failed_n, status=status, llm_calls=llm_calls)
            # 接线点③：批量实际执行完成 → 自产 SCHEDULER_JOB_COMPLETED 事件
            # （内核调度器不发射该事件，由插件自产驱动 auto deep research 联动）。
            # scheduler. 前缀 = 同步分发，处理器仅判断+入队，重活全在队列线程。
            try:
                from plugin_manager.event_bus import EventName, get_event_bus
                get_event_bus().emit(
                    EventName.SCHEDULER_JOB_COMPLETED,
                    job_id="stock_analysis_daily_batch",
                    run_id=run_id,
                    kind=kind,
                    result={"ok": ok_n, "failed": failed_n, "status": status},
                )
            except Exception as err:
                _log.warning("batch completion event emit failed: %s", err)
            # D8：真实发射 plugin.json.hooks.provides 声明的 stock.batch.completed
            # （此前全仓无发射点，属死声明 —— 声明与实现不一致会误导集成方）。
            # 注意通道差异：上面走 event_bus，这里走 hook registry（do_action），
            # 消费者须用 get_event_handlers() 订阅，用 bus.on 收不到。
            try:
                from plugin_manager.hooks import get_hook_registry
                get_hook_registry().do_action(
                    "stock.batch.completed",
                    {"run_id": run_id, "kind": kind, "ok": ok_n,
                     "failed": failed_n, "status": status},
                )
            except Exception as err:
                _log.warning("stock.batch.completed dispatch failed: %s", err)

        return {
            "run_id": run_id,
            "kind": kind,
            "total": len(symbols),
            "ok": ok_n,
            "failed": failed_n,
            "status": ("done" if failed_n == 0 else "degraded") if db_ready else "in-memory",
        }
    finally:
        _release_lock()
