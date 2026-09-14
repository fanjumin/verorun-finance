"""signal_quality.py — 信号兑现回算闭环（P0-2）

只读回算：把 sa_signal_log 的历史信号与其后 T+5/T+20 实际收益对齐，
落 sa_signal_realized，并聚合成命中率/平均收益，供 /api/signal-quality 展示。

红线：只新增只读回算与新表，不改动信号写入路径；收益用后复权（close_hfq），
      规避除权失真（依赖 P0-1）。
"""

import logging
from datetime import date, timedelta

import pandas as pd

try:
    from . import market_calendar as cal
    from . import models_sa as sa
    from .gateway import gateway
except ImportError:  # 顶层脚本运行兜底
    from plugins.stock_analysis import market_calendar as cal
    from plugins.stock_analysis import models_sa as sa
    from plugins.stock_analysis.gateway import gateway

_log = logging.getLogger("stock_analysis.signal_quality")

HORIZONS = (5, 20)          # 前向收益窗口（按序列中"后续第 N 根有效K线"计）


def _forward_cutoff(horizon: int) -> date:
    """往回退 horizon 个交易日：信号日 <= 该值，其前向窗口才算已满。"""
    d = cal.latest_trading_day()
    for _ in range(horizon):
        d = cal.latest_trading_day(d - timedelta(days=1))
    return d


def _anchor(frame: pd.DataFrame, base: date):
    """返回信号日当天K线在序列中的位置；信号日无K线（停牌/缺口）返回 None。"""
    idx = frame.index
    pos = idx.searchsorted(pd.Timestamp(base))
    if pos >= len(idx) or idx[pos].date() != base:
        return None
    return pos


def realize_signals(days_back: int = 90) -> dict:
    """增量回算未兑现信号；串行执行，天然受 gateway 限流约束。"""
    cutoff = _forward_cutoff(max(HORIZONS))
    try:
        sa.ensure_tables()
    except Exception:
        pass
    pending = sa.list_unrealized_signals(days_back=days_back, cutoff=cutoff)
    ok = failed = 0
    realized_today = []          # 【接线点②】收集当日回算结果，尾部统一回流 Reflexion
    col = None
    for sig in pending:
        try:
            symbol, td, kind = sig["symbol"], sig["trade_date"], sig["kind"]
            sigval = sig.get("signal")
            frame = gateway.get_kline(symbol, datalen=max(120, days_back + 40))
            if frame is None or frame.empty:
                failed += 1
                continue
            pos = _anchor(frame, td)
            if pos is None:
                failed += 1
                continue
            col = "close_hfq" if "close_hfq" in frame else "close"

            def close_at(offset):
                j = pos + offset
                if j >= len(frame):
                    return None
                return float(frame[col].iloc[j])

            base_close = close_at(0)
            if not base_close:
                failed += 1
                continue
            ret, hit = {}, {}
            for n in HORIZONS:
                fc = close_at(n)
                if fc is None:
                    ret[n] = None
                    hit[n] = None
                    continue
                r = (fc - base_close) / base_close
                ret[n] = round(r, 4)
                if sigval == "buy":
                    hit[n] = 1 if r > 0 else 0
                elif sigval == "sell":
                    hit[n] = 1 if r < 0 else 0
                else:
                    hit[n] = None          # hold 不判定
            sa.insert_realized(symbol, td, kind, sigval, round(base_close, 4),
                               ret.get(5), ret.get(20), hit.get(5), hit.get(20))
            # 【接线点②】收集当日回算行（signal_log 未落 confidence，缺省 None 可接受）
            realized_today.append({
                "symbol": symbol, "trade_date": td, "kind": kind,
                "signal": sigval,
                "confidence": sig.get("confidence"),
                "ret_5": ret.get(5), "ret_20": ret.get(20),
            })
            ok += 1
        except Exception as err:
            _log.warning("realize %s failed: %s", sig.get("symbol"), err)
            failed += 1
    # 【接线点②】误信号回流 Reflexion；失败静默，不影响回算主链路
    try:
        from .reflexion_feedback import emit_misjudged_reflexions, select_misjudged
        emit_misjudged_reflexions(select_misjudged(realized_today))
    except Exception:
        pass
    return {"cutoff": cutoff.isoformat(), "pending": len(pending),
            "ok": ok, "failed": failed}


def quality_summary(days: int = 90) -> list:
    return sa.get_realized_summary(days)
