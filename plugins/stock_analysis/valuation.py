# valuation.py — Tushare 深度估值：PE(TTM) / PB 近 5 年历史分位（阶段2）
# 仅当 token 可用时调用；任何失败返回 None，调用方静默降级，零影响主链路。
# 合规：估值数据源为 tushare（authorized=True），由调用方并入 data_sources。
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

_log = logging.getLogger("stock_analysis.valuation")

try:
    from . import tushare_client as tsc
    from .providers.tushare_provider import to_ts_code
except ImportError:                       # 脚本直接运行兜底
    import tushare_client as tsc
    from providers.tushare_provider import to_ts_code

_YEARS = 5                                # 估值分位回看窗口（近 5 年）


def pe_pb_percentile(symbol: str) -> dict:
    """返回当前 PE(TTM)/PB 在近 5 年历史序列中的分位；token 缺失/无权限/接口失败返回 None。

    分位语义：pe_percentile_5y = 当前值低于历史样本的比例（越小越低估）。
    """
    code = to_ts_code(symbol)
    end = datetime.now()
    start = end - timedelta(days=_YEARS * 366 + 30)
    try:
        pro = tsc.get_pro()
        df = pro.daily_basic(ts_code=code,
                             start_date=start.strftime("%Y%m%d"),
                             end_date=end.strftime("%Y%m%d"),
                             fields="trade_date,pe,pe_ttm,pb")
    except Exception as err:
        _log.warning("valuation fetch failed %s: %s", symbol, err)
        return None
    if df is None or df.empty:
        _log.warning("valuation empty for %s", symbol)
        return None
    df = df.sort_values("trade_date")
    pe_series = pd.to_numeric(df["pe_ttm"], errors="coerce").dropna()
    pb_series = pd.to_numeric(df["pb"], errors="coerce").dropna()
    if pe_series.empty or pb_series.empty:
        return None
    cur_pe = float(pe_series.iloc[-1])
    cur_pb = float(pb_series.iloc[-1])

    def _percentile(series: pd.Series, cur: float) -> float:
        return round(float((series <= cur).mean()) * 100, 1)

    return {
        "pe_ttm": round(cur_pe, 2),
        "pb": round(cur_pb, 2),
        "pe_percentile_5y": _percentile(pe_series, cur_pe),
        "pb_percentile_5y": _percentile(pb_series, cur_pb),
        "pe_min_5y": round(float(pe_series.min()), 2),
        "pe_max_5y": round(float(pe_series.max()), 2),
        "pb_min_5y": round(float(pb_series.min()), 2),
        "pb_max_5y": round(float(pb_series.max()), 2),
        "window_years": _YEARS,
        "samples": int(len(pe_series)),
    }


def valuation_line(val: dict) -> str:
    """报告行：PE(TTM) 处于近 5 年 x% 分位（低估/合理/高估区）。"""
    if not val:
        return ""
    pct = val.get("pe_percentile_5y")
    if pct is None:
        return ""
    band = "低估区" if pct < 30 else ("高估区" if pct > 70 else "合理区")
    return f"PE(TTM) {val['pe_ttm']} 处于近 5 年 {pct}% 分位（{band}）"
