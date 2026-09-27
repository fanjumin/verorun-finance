"""
corporate_actions.py — 复权与股本事件引擎（P0）

为什么这是最高优先级
--------------------
不复权的价格序列在除权除息日会出现假跌幅（10送10 → -50%），
导致：动量因子失效、技术形态失真、信号兑现统计全错、回测曲线不可信。

口径（写死，不随版本变）
------------------------
raw  不复权：真实成交价。用于判断涨跌停、真实成交金额。
qfq  前复权：以**最新**价为锚，factor[last] = 1。用于画 K 线、看形态、算技术指标。
hfq  后复权：以**首日**价为锚，factor[0] = 1。用于算收益率、回测、累计涨幅。

调整比率（除权除息日 ex_date，前一交易日收盘 P）：
    r = (P − D) / (P × S)
    D = 每股现金分红（税前）  S = 送转后股数 / 送转前股数（10送10 → 2.0）

成交量反向调整：adj_vol = raw_vol / factor（qfq 口径）
    —— 价格被缩小多少倍，成交量就要放大多少倍，否则成交额对不上。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

_log = logging.getLogger("stock_analysis.corporate_actions")


# ------------------------------------------------------------------ 事件


@dataclass(frozen=True)
class CorpAction:
    ex_date: pd.Timestamp          # 除权除息日（该日起买入不再享有权益）
    cash_div: float = 0.0          # 每股现金分红（税前，元）
    split_ratio: float = 1.0       # 送转后 / 送转前（10送10 → 2.0；10转5 → 1.5）
    rights_ratio: float = 0.0      # 配股比例（10配3 → 0.3），0 表示无配股
    rights_price: float = 0.0      # 配股价
    action_type: str = "dividend"  # dividend / split / rights / spinoff

    def adjust_ratio(self, prev_close: float) -> float:
        """返回价格调整系数 r：除权除息日之前的价格乘以 r 后与之后可比。"""
        if prev_close <= 0:
            return 1.0
        if self.rights_ratio > 0 and self.rights_price > 0:
            # 配股：除权价 = (P + 配股价 × 配股比例) / (1 + 配股比例)
            price = (prev_close + self.rights_price * self.rights_ratio) / (1 + self.rights_ratio)
        else:
            price = prev_close
        numerator = price - self.cash_div
        denominator = price * (self.split_ratio if self.split_ratio > 0 else 1.0)
        if denominator <= 0:
            return 1.0
        return float(numerator / denominator)


# ------------------------------------------------------------------ 引擎


class AdjustmentEngine:
    """
    用法
    ----
        eng = AdjustmentEngine()
        bars = eng.apply(raw_df, events)   # raw_df: index=date, cols=[open,high,low,close,vol]

    返回在原 DataFrame 上追加列：
        factor_qfq / factor_hfq
        open_qfq / high_qfq / low_qfq / close_qfq / vol_qfq
        open_hfq / high_hfq / low_hfq / close_hfq / vol_hfq
    """

    PRICE_COLS = ("open", "high", "low", "close")

    # ---------------- 因子构建 ----------------

    def build_factors(
        self,
        dates: Sequence[pd.Timestamp],
        events: Iterable[CorpAction],
        prev_close_by_date: Optional[pd.Series] = None,
        mode: str = "qfq",
    ) -> pd.Series:
        dates = pd.DatetimeIndex(pd.to_datetime(dates)).sort_values()
        ratio: dict[pd.Timestamp, float] = {}

        for ev in sorted(events, key=lambda e: e.ex_date):
            ex = pd.Timestamp(ev.ex_date)
            if ex not in dates:
                # 除权日若非交易日，顺延到下一个交易日
                nxt = dates[dates > ex]
                if len(nxt) == 0:
                    continue
                ex = nxt[0]
            pc = self._prev_close(dates, ex, prev_close_by_date)
            if pc is None:
                continue
            # 同一天多个事件（如分红+送转）累乘
            ratio[ex] = ratio.get(ex, 1.0) * ev.adjust_ratio(pc)

        f = pd.Series(1.0, index=dates, dtype="float64")
        if mode == "qfq":
            # 从后往前累乘；最后一个交易日 factor = 1（锚定最新价）
            for i in range(len(dates) - 2, -1, -1):
                f.iloc[i] = f.iloc[i + 1] * ratio.get(dates[i + 1], 1.0)
        elif mode == "hfq":
            # 从前往后累除；第一个交易日 factor = 1（锚定首日价）
            for i in range(1, len(dates)):
                f.iloc[i] = f.iloc[i - 1] / ratio.get(dates[i], 1.0)
        else:
            raise ValueError(f"未知复权模式: {mode}")
        return f

    @staticmethod
    def _prev_close(dates, ex, prev_close_by_date) -> Optional[float]:
        """取除权日前一交易日的收盘价。无价格序列时返回 None（该事件跳过）。"""
        if prev_close_by_date is None:
            return None
        prior = prev_close_by_date.loc[:ex].dropna()
        if len(prior) == 0:
            return None
        # 若除权日本身有收盘价，取倒数第二个；否则取最后一个
        return float(prior.iloc[-2] if len(prior) >= 2 else prior.iloc[-1])

    # ---------------- 应用 ----------------

    def apply(self, df: pd.DataFrame, events: Iterable[CorpAction]) -> pd.DataFrame:
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

        prev_close = df["close"] if "close" in df.columns else None
        events = list(events)

        qfq = self.build_factors(df.index, events, prev_close, mode="qfq")
        hfq = self.build_factors(df.index, events, prev_close, mode="hfq")

        out = df.copy()
        out["factor_qfq"] = qfq
        out["factor_hfq"] = hfq

        for mode, f in (("qfq", qfq), ("hfq", hfq)):
            for c in self.PRICE_COLS:
                if c in out.columns:
                    out[f"{c}_{mode}"] = out[c] * f
            if "vol" in out.columns:
                # 成交量反向调整（价格被缩小 → 成交量需放大）
                out[f"vol_{mode}"] = out["vol"] / f.replace(0, np.nan)
            if "amount" in out.columns:
                out[f"amount_{mode}"] = out.get(f"close_{mode}", out["close"] * f) * out.get(f"vol_{mode}", out["vol"])
        return out

    # ---------------- 校验（写进 CI） ----------------

    @staticmethod
    def validate(df: pd.DataFrame, tol: float = 1e-6) -> List[str]:
        """返回问题列表；空列表表示通过。四条验收标准见实施方案 §3.3。"""
        errs: List[str] = []
        if "close_qfq" not in df.columns or "close_hfq" not in df.columns:
            return ["缺少复权列，请先调用 apply()"]

        # 1) qfq 最新价 == 原始最新价
        if abs(df["close_qfq"].iloc[-1] - df["close"].iloc[-1]) > tol * max(1.0, abs(df["close"].iloc[-1])):
            errs.append("qfq 最新价与原始价不一致（锚点错误）")
        # 2) hfq 首日价 == 原始首日价
        if abs(df["close_hfq"].iloc[0] - df["close"].iloc[0]) > tol * max(1.0, abs(df["close"].iloc[0])):
            errs.append("hfq 首日价与原始价不一致（锚点错误）")
        # 3) 复权因子单调性：qfq 非递减（越早越小），hfq 非递减
        if not (df["factor_qfq"].diff().dropna() >= -tol).all():
            errs.append("factor_qfq 非单调（事件日期顺序可能有问题）")
        # 4) 复权后不应再出现除权式假跌（>40% 单日跌幅视为可疑，ST/退市另设豁免名单）
        ret = df["close_qfq"].pct_change().dropna()
        suspicious = ret[ret < -0.40]
        if len(suspicious) > 0:
            errs.append(
                "复权后仍存在 >40% 单日跌幅，疑似事件数据缺失："
                + ", ".join(str(d.date()) for d in suspicious.index[:5])
            )
        return errs


# ------------------------------------------------------------------ 外部源适配


def from_tushare(rows: List[dict]) -> List[CorpAction]:
    """Tushare `dividend` / `adj_factor` 记录 → CorpAction 列表。

    Tushare 字段：ex_date, cash_div_tax, stk_div, stk_bo_rate, stk_co_rate,
                  cash_div_pre, div_proc（实施进度）
    注意：只取 div_proc == '实施' 的记录，预案会反复修改导致因子跳变。
    """
    out: List[CorpAction] = []
    for r in rows:
        if str(r.get("div_proc", "实施")) not in ("实施", "", "None"):
            continue
        ex = r.get("ex_date")
        if not ex:
            continue
        cash = float(r.get("cash_div_tax") or r.get("cash_div_pre") or 0.0)
        # stk_div 是每 10 股送转股数；10送10 → 每10股送10股 → ratio = (10+10)/10 = 2.0
        stk = float(r.get("stk_div") or 0.0) / 10.0
        bo = float(r.get("stk_bo_rate") or 0.0) / 10.0    # 转增
        co = float(r.get("stk_co_rate") or 0.0) / 10.0    # 送股
        split = 1.0 + stk + bo + co
        out.append(
            CorpAction(
                ex_date=pd.Timestamp(ex),
                cash_div=cash,
                split_ratio=split if split > 0 else 1.0,
                action_type="split" if split != 1.0 else "dividend",
            )
        )
    return out


# ------------------------------------------------------------------ 事件装载（DB）


def load_events(symbol: str) -> List[CorpAction]:
    """从 sa_corp_action 读取某标的的分红送转事件（插件自有 schema，不触外网）。

    设计：复权是"数据口径"而非"数据内容"，必须可缓存、可审计、可回溯版本，
    因此落库读取；缺失时返回空列表由调用方 fail-open（保持 raw 并如实标注口径）。
    """
    try:
        from .models_sa import get_db
        with get_db(count_sink=False) as conn:
            rows = conn.execute(
                "SELECT ex_date, cash_div, split_ratio, rights_ratio, rights_price, "
                "action_type FROM sa_corp_action "
                "WHERE symbol = ? ORDER BY ex_date ASC", (symbol,)).fetchall()
    except Exception as err:                      # 表不存在/DB 不可用 → 降级
        _log.warning("corp_action load failed %s: %s", symbol, err)
        return []
    return [CorpAction(ex_date=pd.Timestamp(r["ex_date"]),
                       cash_div=float(r["cash_div"] or 0.0),
                       split_ratio=float(r["split_ratio"] or 1.0),
                       rights_ratio=float(r["rights_ratio"] or 0.0),
                       rights_price=float(r["rights_price"] or 0.0),
                       action_type=str(r["action_type"] or "dividend"))
            for r in rows]


# ------------------------------------------------------------------ 自检


if __name__ == "__main__":
    # 构造：10 个交易日，第 6 天 10送10 + 每10股派5元（即每股 0.5 元）
    idx = pd.bdate_range("2026-01-01", periods=10)
    close = [100, 101, 102, 103, 104, 55, 56, 57, 58, 59]   # 除权后价格腰斩
    df = pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "vol": [1_000_000] * 10,
    }, index=idx)

    ev = [CorpAction(ex_date=idx[5], cash_div=0.5, split_ratio=2.0)]
    eng = AdjustmentEngine()
    out = eng.apply(df, ev)

    print(out[["close", "close_qfq", "close_hfq", "factor_qfq", "vol_qfq"]].round(4))
    print("校验:", eng.validate(out) or "通过")
    # 期望：
    #   qfq 最新价 = 59（锚定最新），除权日前价格 ≈ 52 左右
    #   hfq 首日价 = 100，除权日后价格 ≈ 110+
    #   复权前后日收益率连续（除权日不再是 -47%）
