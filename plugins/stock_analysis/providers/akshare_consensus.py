# providers/akshare_consensus.py — A 股一致预期免费源（同花顺盈利预测）
#
# 背景：内核 CONSENSUS 原只有 FMP（美股，需 key）与 Tushare（A 股 forecast_vip，
# 需 2000 积分）。120 积分账号两项都拿不到 → 一致预期永久空态。
# 本 provider 用 akshare 免费源补齐 A 股，优先级排在前两者之后。
#
# 数据边界（实测 2026-09-19，见 scripts/_probe_s4.out.txt）：
#   - 只有 **每股收益（年度预测）** 与 **预测机构数**，外加最小值/均值/最大值/行业平均数。
#     没有营收、净利润、目标价 —— 不编字段，缺的就不给。
#   - 只覆盖 A 股六位代码；非 A 股明确抛 ProviderUnavailable（★ 必须是这个而不是
#     ProviderError：后者会被 BaseProviderV2.fetch 包装并丢掉 retryable，
#     误触发 COOLDOWN 会把整条链冻住，见 akshare_macro 同款处理）。
#
# 接口：ak.stock_profit_forecast_ths(symbol="600519", indicator="预测年报每股收益")
#   返回列：年度 / 预测机构数 / 最小值 / 均值 / 最大值 / 行业平均数
#   实测 6 个 A 股标的全部返回 3 行（FY1~FY3），单次 1~7s；
#   不存在的代码抛 ValueError("No tables found")，非 A 股代码抛 XMLSyntaxError。
import re
import time

from .base import DataCategory
from .base_v2 import BaseProviderV2, FetchResult, ProviderUnavailable

import logging

_log = logging.getLogger("stock_analysis.akshare_consensus")

INDICATOR = "预测年报每股收益"          # 同花顺指标名（唯一稳定可用的免费口径）


def _plain(symbol: str) -> str:
    """sh600519 / CN:600519 / 600519.SH → 600519"""
    s = (symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.lower()
    if len(s) > 6 and s[:2] in ("sh", "sz", "bj"):
        s = s[2:]
    return s.split(".")[0]


def _unsupported(msg: str) -> ProviderUnavailable:
    err = ProviderUnavailable(f"[akshare_consensus/consensus] {msg}")
    err.retryable = False            # 不支持 = 永久，不该进冷却
    return err


def _f(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


class AkshareConsensusProvider(BaseProviderV2):
    name = "akshare_consensus"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.CONSENSUS})
    rate_per_min = 12
    burst = 2

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is not DataCategory.CONSENSUS:
            raise NotImplementedError(cat)
        plain = _plain(symbol)
        if not re.fullmatch(r"\d{6}", plain):
            raise _unsupported(f"仅支持 A 股六位代码（收到 {symbol!r}）")
        data = self._fetch_consensus(plain)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    # ── 内部实现 ────────────────────────────────────────
    @staticmethod
    def _import_ak():
        try:
            import akshare as ak
            return ak
        except ImportError as err:
            raise ProviderUnavailable(f"[akshare_consensus] akshare not installed: {err}")

    def _fetch_consensus(self, plain: str) -> dict:
        ak = self._import_ak()
        try:
            df = ak.stock_profit_forecast_ths(symbol=plain, indicator=INDICATOR)
        except Exception as err:
            # 无覆盖（ValueError: No tables found）/ 上游异常都算本源不可用，
            # 交给链上下一源，不在本源里重试。
            raise _unsupported(f"同花顺预测不可达或无覆盖（{plain}）：{err}")

        if df is None or df.empty or "年度" not in df.columns:
            raise _unsupported(f"同花顺预测返回空（{plain}）")

        rows = []
        for _, r in df.iterrows():
            year = _f(r.get("年度"))
            if year is None:
                continue
            rows.append({
                "year": int(year),
                "institutions": int(_f(r.get("预测机构数")) or 0),
                "eps_mean": _f(r.get("均值")),
                "eps_min": _f(r.get("最小值")),
                "eps_max": _f(r.get("最大值")),
                "industry_avg": _f(r.get("行业平均数")),
            })
        rows.sort(key=lambda x: x["year"])
        if not rows:
            raise _unsupported(f"同花顺预测无有效年度行（{plain}）")

        return {
            "source": "akshare_consensus",
            "symbol": plain,
            "metric": "eps",
            "indicator": INDICATOR,
            "unit": "CNY/share",
            "forecasts": rows,
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
