"""kline_service.py — 桌面端行情端点数据组装层（契约 §3：/api/kline 与 /api/quotes）。

服务端权威口径：
- K 线经 gateway.get_kline 获取（当前仅日线；weekly/monthly/分钟线未接入 → UnsupportedPeriodError）；
- 指标经 indicators.compute_indicators 计算（iv-3），与 bars 等长对齐；
- 返回契约 envelope {ok, data, error, meta}。

已知边界（契约 §3 校准）：
- amount：现有数据源无成交额列，bars.amount 一律置 null；
- period：gateway 当前仅支持 daily，其余周期返回 501（不造假数据）；
- offset/until：gateway 仅支持尾部取数，仅能覆盖近期历史翻页；
  深度历史游标（until 早于已取窗口）返回尽可能多的 bars，由桌面端数据层按需扩展。
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime

import pandas as pd

from .gateway import DataCategory, gateway
from .indicators import INDICATOR_VERSION, _round, compute_indicators

_log = logging.getLogger("stock_analysis.kline_service")

MAX_LIMIT = 2000       # 契约 §3：limit ≤ 2000
MAX_OFFSET = 5000      # 服务端保护：防无界取数
MAX_QUOTES = 50        # 契约 §3：symbols ≤ 50
WARMUP = 60            # 指标暖机前置条数（MA60 需要 60 根历史，切片后首个 bar 已收敛）

_SUPPORTED_PERIODS = {"daily"}
# 契约列出但网关尚未接入的周期：诚实校准，501 而非假数据
_UNSUPPORTED_PERIODS = {"weekly", "monthly", "60m", "30m", "15m", "5m"}

_BASIS_NOTES = {"hfq": "后复权价（hfq）", "raw": "不复权价（raw）"}


class UnsupportedPeriodError(ValueError):
    """请求了网关未接入的周期（weekly/monthly/分钟线）。"""


class KlineUnavailableError(RuntimeError):
    """K 线数据不可用（无数据 / 数据源全链路失败 / until 越界）。"""


class QuoteUnavailableError(RuntimeError):
    """行情快照全量失败。"""


def _int_or_none(v) -> int | None:
    """float → int；NaN/Inf/None → None（JSON null）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(round(f))


def _resolve_basis(frame: pd.DataFrame, adjust: str) -> str:
    """校准后的展示基准。raw 请求遇到 hfq-only 降级帧（_raw_unavailable）时如实回退 hfq。"""
    if adjust == "hfq" and "close_hfq" in frame.columns:
        return "hfq"
    if adjust == "raw" and "_raw_unavailable" not in frame.columns:
        return "raw"
    return "hfq" if "close_hfq" in frame.columns else "raw"


def _base_columns(frame: pd.DataFrame, basis: str) -> tuple[str, str, str, str]:
    """按展示基准选择 K 线 OHLC 列名。"""
    if basis == "hfq" and "close_hfq" in frame.columns:
        return "open_hfq", "high_hfq", "low_hfq", "close_hfq"
    return "open", "high", "low", "close"


def _slice_ind(val, start: int, end: int):
    """递归切片指标容器（标量数组或 macd/kdj/boll 子对象）。"""
    if isinstance(val, dict):
        return {k: _slice_ind(v, start, end) for k, v in val.items()}
    return val[start:end]


def _kline_source() -> str | None:
    """从 gateway 线程级 usage 快照取本次 K 线实际来源。"""
    for entry in reversed(gateway.usage_snapshot()):
        if entry.get("category") == "kline":
            return entry.get("source")
    return None


def kline_payload(symbol: str, period: str = "daily", adjust: str = "hfq",
                  limit: int = 120, offset: int = 0,
                  until: str | None = None) -> dict:
    """组装 /api/kline 的契约 envelope。异常：UnsupportedPeriodError / ValueError / KlineUnavailableError。"""
    period = (period or "daily").lower()
    if period not in _SUPPORTED_PERIODS:
        raise UnsupportedPeriodError(
            f"period '{period}' not supported yet (gateway currently daily only)")
    adjust = (adjust or "hfq").lower()
    if adjust not in ("hfq", "raw"):
        raise ValueError("adjust must be 'hfq' or 'raw'")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, min(int(offset), MAX_OFFSET))

    # 取数含 WARMUP 前置：指标在完整窗口上计算，切片后的首个 bar 已过暖机
    try:
        frame = gateway.get_kline(symbol, datalen=offset + limit + WARMUP)
    except Exception as err:
        raise KlineUnavailableError(f"kline unavailable: {err}") from err
    if frame is None or frame.empty:
        raise KlineUnavailableError("no kline data")
    if until:
        frame = frame[frame.index <= pd.to_datetime(until)]
        if frame.empty:
            raise KlineUnavailableError(f"no kline data before {until}")

    basis = _resolve_basis(frame, adjust)
    indicators = compute_indicators(frame, basis=basis)

    end = max(0, len(frame) - offset) if offset > 0 else len(frame)
    start = max(0, end - limit)
    window = frame.iloc[start:end]

    o, h, l, c = _base_columns(window, basis)
    bars = []
    for idx, row in window.iterrows():
        bars.append({
            "date": str(pd.Timestamp(idx).date()),
            "o": _round(row[o]), "h": _round(row[h]),
            "l": _round(row[l]), "c": _round(row[c]),
            "vol": _int_or_none(row.get("volume")),
            "amount": None,          # 数据源无成交额列，契约 §3 校准为 null
        })

    return {
        "ok": True,
        "data": {
            "bars": bars,
            "indicators": _slice_ind(indicators, start, end),
            "basis": basis,
            "basis_note": _BASIS_NOTES.get(basis, basis),
        },
        "error": None,
        "meta": {
            "data_date": bars[-1]["date"] if bars else None,
            "source": _kline_source(),
            "price_basis": basis,
            "indicator_version": INDICATOR_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stale": False,
        },
    }


def _to_quote(symbol: str, q: dict) -> dict:
    """腾讯快照字段 → 契约 quote 对象。

    历史缺陷：open/high/low/volume/amount 五个字段**硬编码 None**，注释称
    「快照源不提供」。但腾讯 88 字段里全部有（[5]今开 [33]最高 [34]最低
    [6]成交量手 [37]成交额万元），providers/tencent.py 也已补齐 →
    曾导致前端行情条/K线页这些列恒为 null。此处改为透传，缺失才为 None。
    """
    price = float(q.get("price") or 0)
    prev_close = float(q.get("prev_close") or 0)

    def _opt(key: str) -> float | None:
        """0 视为缺失（盘前/停牌时腾讯给 0），避免把 0 当真实值展示。"""
        v = q.get(key)
        if v in (None, "", 0, 0.0):
            return None
        try:
            return _round(float(v))
        except (TypeError, ValueError):
            return None

    return {
        "symbol": symbol,
        "name": q.get("name"),
        "price": _round(price),
        "change": _round(price - prev_close) if prev_close else None,
        "change_pct": _round(q.get("change_pct")),
        "volume": _opt("volume"),
        "amount": _opt("amount"),
        "pe": _round(q.get("pe_ttm")),
        "pb": _round(q.get("pb")),
        "turnover": _round(q.get("turnover_rate")),
        "high": _opt("high"),
        "low": _opt("low"),
        "open": _opt("open"),
        "prev_close": _round(prev_close),
        "at": time.strftime("%H:%M:%S"),
    }


def quotes_payload(symbols: list[str], fields: str | None = None) -> dict:
    """组装 /api/quotes 的契约 envelope。异常：QuoteUnavailableError。

    fields 参数当前仅作契约占位：快照源已全量返回，客户端按需取字段。
    """
    quotes = []
    for symbol in symbols:
        try:
            q = gateway.get_quote(symbol, DataCategory.QUOTE)
        except Exception as err:
            # 单标的失败跳过并留日志（TTL 60s，瞬时故障自愈），全量失败才抛错
            _log.warning("quote unavailable symbol=%s: %s", symbol, err)
            continue
        quotes.append(_to_quote(symbol, q))
    if not quotes:
        raise QuoteUnavailableError("quotes unavailable")

    from . import market_calendar as cal
    try:
        data_date = str(cal.latest_trading_day())
    except Exception:
        data_date = datetime.now().strftime("%Y-%m-%d")

    return {
        "ok": True,
        "data": {"quotes": quotes},
        "error": None,
        "meta": {
            "data_date": data_date,
            "source": "tencent",
            "price_basis": None,           # 快照无复权概念
            "indicator_version": None,     # 快照不涉及指标口径
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stale": False,
        },
    }
