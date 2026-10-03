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

    展示基准为 hfq/qfq 且存在对应 *_hfq/*_qfq 列时，用该复权序列计算；否则回退 raw。
    """
    suffix = ("_qfq" if (basis == "qfq" and "close_qfq" in frame)
              else ("_hfq" if (basis == "hfq" and "close_hfq" in frame) else ""))
    close = frame[f"close{suffix}"]
    high = frame[f"high{suffix}"]
    low = frame[f"low{suffix}"]
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


# ── 指标矩阵（桌面端个股工作台首屏 8 行）─────────────────────────────
# 2026-09-21 架构整改：判定口径自桌面端 StockWorkbenchV3.buildMatrix() 上移至此。
# 此前壳层自算 MA/MACD/KDJ/RSI/BOLL 的结论并自算「量比」，等于在插件之外复制了
# 一份指标口径 —— 阈值（KDJ 80/20、RSI 70/30）与量比分母口径都可能与
# indicators.py 的权威实现漂移（内核 contract §3：桌面端只渲染不计算）。
#
# 断言纪律（沿用原实现）：只做「最新一根 K 线上的指标值 + 相对关系」判定，
# 不含任何预测性表述 —— 内核没有预测能力，UI 不暗示。

#: 矩阵行数（MA5/MA20/MA60/MACD/KDJ/RSI/BOLL/量比），供测试断言
MATRIX_ROW_COUNT = 8


def _last(seq) -> Optional[float]:
    """取序列中最后一个非空值（暖机期在头部，尾部通常已有值）。"""
    if not seq:
        return None
    for v in reversed(seq):
        if v is not None:
            return float(v)
    return None


def build_matrix(bars: list, indicators: dict) -> list:
    """派生指标矩阵 8 行。

    返回 [{"name","value","reference","status","verdict","code"}]：
      * value      —— 两位小数字符串，无数据为 '--'
      * status     —— 中文口径文案（与既有 UI 文案逐字一致，避免观感变化）
      * code       —— 机器可读判定（前端可按需 i18n / 过滤）
      * verdict    —— bull | bear | flat

    量比口径：当日量 ÷ 近 5 日均量（不含当日）；不足 6 根或均量为 0 → 数据不足。
    """
    ind = indicators or {}
    close = (bars[-1].get("c") if bars else None)
    close = float(close) if close is not None else None

    rows: list = []

    def push(name: str, value: Optional[float], reference: str,
             status: str, verdict: str, code: str) -> None:
        rows.append({
            "name": name,
            "value": "--" if value is None else f"{value:.2f}",
            "reference": reference,
            "status": status,
            "verdict": verdict,
            "code": code,
        })

    # ── MA：价格与均线的相对位置 ──
    def ma_row(name: str, value: Optional[float]) -> None:
        if close is None or value is None:
            push(name, value, "--", "数据不足", "flat", "insufficient")
        elif close > value:
            push(name, value, f"{close:.2f}", "价在上方", "bull", "price_above")
        else:
            push(name, value, f"{close:.2f}", "价在下方", "bear", "price_below")

    ma_row("MA5", _last(ind.get("ma5")))
    ma_row("MA20", _last(ind.get("ma20")))
    ma_row("MA60", _last(ind.get("ma60")))

    # ── MACD：DIF 与 DEA 的交叉 ──
    macd_d = ind.get("macd") or {}
    dif, dea = _last(macd_d.get("dif")), _last(macd_d.get("dea"))
    hist = _last(macd_d.get("hist"))
    macd_ref = "--" if dea is None else f"{dea:.3f}"
    if dif is None or dea is None:
        push("MACD", dif, macd_ref, "数据不足", "flat", "insufficient")
    elif dif > dea:
        push("MACD", dif, macd_ref, "金叉向上", "bull", "golden_cross")
    else:
        push("MACD", dif, macd_ref, "死叉向下", "bear", "dead_cross")
    if hist is not None:
        rows[-1]["reference"] = f"{rows[-1]['reference']} · H{hist:.3f}"

    # ── KDJ：K 值超买/超卖 ──
    kdj_d = ind.get("kdj") or {}
    k_val, d_val, j_val = (_last(kdj_d.get("k")), _last(kdj_d.get("d")),
                           _last(kdj_d.get("j")))
    kdj_ref = "--" if d_val is None else f"D {d_val:.1f}" + (
        f" · J {j_val:.1f}" if j_val is not None else "")
    if k_val is None:
        push("KDJ", k_val, kdj_ref, "数据不足", "flat", "insufficient")
    elif k_val > 80:
        push("KDJ", k_val, kdj_ref, "超买区", "bear", "overbought")
    elif k_val < 20:
        push("KDJ", k_val, kdj_ref, "超卖区", "bull", "oversold")
    else:
        push("KDJ", k_val, kdj_ref, "中性区", "flat", "neutral")

    # ── RSI(14)：超买/超卖 ──
    rsi14 = _last(ind.get("rsi14"))
    if rsi14 is None:
        push("RSI", None, "14 日", "数据不足", "flat", "insufficient")
    elif rsi14 > 70:
        push("RSI", rsi14, "14 日", "超买", "bear", "overbought")
    elif rsi14 < 30:
        push("RSI", rsi14, "14 日", "超卖", "bull", "oversold")
    else:
        push("RSI", rsi14, "14 日", "中性", "flat", "neutral")

    # ── BOLL：价格与上下轨 ──
    boll_d = ind.get("boll") or {}
    mid, up, low = (_last(boll_d.get("mid")), _last(boll_d.get("up")),
                    _last(boll_d.get("low")))
    boll_ref = f"{low:.2f}–{up:.2f}" if (up is not None and low is not None) else "--"
    if close is None or up is None or low is None:
        push("BOLL", mid, boll_ref, "数据不足", "flat", "insufficient")
    elif close > up:
        push("BOLL", mid, boll_ref, "上穿上轨", "bull", "upper_break")
    elif close < low:
        push("BOLL", mid, boll_ref, "下破下轨", "bear", "lower_break")
    else:
        push("BOLL", mid, boll_ref, "轨内运行", "flat", "in_band")

    # ── 量比：当日量 ÷ 近 5 日均量（不含当日）──
    vols = [b.get("vol") for b in bars if b.get("vol") is not None]
    ratio = None
    if len(vols) >= 6 and vols[-1] and float(vols[-1]) > 0:
        prev5 = vols[-6:-1]
        avg = sum(float(v) for v in prev5) / len(prev5)
        if avg > 0:
            ratio = float(vols[-1]) / avg
    if ratio is None:
        push("量比", None, "近 5 日均量", "数据不足", "flat", "insufficient")
    elif ratio > 1.5:
        push("量比", ratio, "近 5 日均量", "显著放量", "bull", "vol_surge")
    elif ratio > 1:
        push("量比", ratio, "近 5 日均量", "温和放量", "bull", "vol_up")
    else:
        push("量比", ratio, "近 5 日均量", "缩量", "bear", "vol_shrink")

    return rows
