"""indicators.py — 服务端权威技术指标计算（契约 §3：桌面端只渲染不计算）。

口径对齐 stock_skill._technical_snapshot（后复权 close_hfq 信号空间），
输出与 K 线 bars 等长对齐的数组，暖机期空值置 null（JSON None）。
indicator_version 供客户端缓存失效（契约 meta.indicator_version）。

一致性约定：
- MA / MACD / RSI / BOLL 基于展示基准（hfq 用 close_hfq，raw 用 close）计算，保证与图表叠加对齐；
- KDJ 使用同一基准的 high/low/close（RSV 为相对位置，与绝对价位无关）。
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np
import pandas as pd

INDICATOR_VERSION = "iv-4"


def _round(v) -> Optional[float]:
    """float → 4 位小数；NaN/Inf/None → None（JSON null）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 4)


def _as_list(series: pd.Series) -> List[Optional[float]]:
    return [_round(x) for x in series.tolist()]


def ma(close: pd.Series, n: int) -> List[Optional[float]]:
    """简单移动平均，暖机期 null。"""
    return _as_list(close.rolling(n).mean())


def macd(close: pd.Series) -> dict:
    """MACD：EMA12-EMA26，DEA=EMA9(DIF)，HIST=DIF-DEA（adjust=False 对齐现有口径）。"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea
    return {
        "dif": _as_list(dif),
        "dea": _as_list(dea),
        "hist": _as_list(hist),
    }


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9) -> dict:
    """KDJ(9,3,3)，经典 SMA(X,3,1) 递推；数据不足 9 根时为 null。"""
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    # DEF-01：pandas>=3 禁止 pd.NA → float64 标量构造（NAType），改用 np.nan。
    # 占位值在下方循环逐行被覆盖，仅作容器初始化，数值口径不变（iv-4）。
    k = pd.Series(np.nan, index=close.index, dtype="float64")
    d = pd.Series(np.nan, index=close.index, dtype="float64")
    k_val = d_val = 50.0
    for i in close.index:
        v = rsv.loc[i]
        if pd.notna(v):
            k_val = k_val * 2 / 3 + float(v) / 3
            d_val = d_val * 2 / 3 + k_val / 3
        k.loc[i] = k_val
        d.loc[i] = d_val
    j = 3 * k - 2 * d
    return {"k": _as_list(k), "d": _as_list(d), "j": _as_list(j)}


def rsi(close: pd.Series, n: int = 14) -> List[Optional[float]]:
    """Wilder 平滑 RSI(14)（第三方终端同口径）。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return _as_list(100 - 100 / (1 + rs))


def boll(close: pd.Series, n: int = 20, k: float = 2.0) -> dict:
    """布林带(20, 2)，总体标准差（ddof=0）。"""
    mid = close.rolling(n).mean()
    std = close.rolling(n).std(ddof=0)
    return {
        "mid": _as_list(mid),
        "up": _as_list(mid + k * std),
        "low": _as_list(mid - k * std),
    }


def compute_indicators(frame: pd.DataFrame, basis: str = "hfq") -> dict:
    """对 K 线 frame 计算全套指标，数组与 bars 等长对齐。

    basis='hfq' 且存在 *_hfq 列时使用复权序列，否则回退 raw。
    """
    close = frame["close_hfq"] if (basis == "hfq" and "close_hfq" in frame) else frame["close"]
    high = frame["high_hfq"] if (basis == "hfq" and "high_hfq" in frame) else frame["high"]
    low = frame["low_hfq"] if (basis == "hfq" and "low_hfq" in frame) else frame["low"]
    return {
        "ma5": ma(close, 5),
        "ma10": ma(close, 10),
        "ma20": ma(close, 20),
        "ma60": ma(close, 60),
        "macd": macd(close),
        "kdj": kdj(high, low, close),
        "rsi14": rsi(close, 14),
        "boll": boll(close, 20, 2.0),
    }
