# backtest_runner.py — 回测编排层：连接 gateway 数据 → factor_lab 计算 → 结果存储
# P1-W10：封装 BacktestConfig 六条铁律的数据准备与可交易掩码构建。
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from .factor_lab import (
    BacktestConfig,
    FACTOR_REGISTRY,
    ic_series,
    factor_report,
    perf_metrics,
    quintile_backtest,
)
from .gateway import DataGateway
from .models_sa import get_classification

_log = logging.getLogger("stock_analysis.backtest_runner")


class BacktestRunner:
    """回测编排器。

    用法:
        runner = BacktestRunner()
        result = runner.run_quintile(
            universe=["CN:600519", "CN:000858", ...],
            factor_name="mom_20",
            datalen=250,
        )
    """

    def __init__(self, gateway: Optional[DataGateway] = None):
        self._gw = gateway or DataGateway()

    def fetch_universe(self, universe: Sequence[str],
                       datalen: int = 250) -> dict[str, pd.DataFrame]:
        """批量拉取 K 线，返回 {symbol: DataFrame}。失败的 symbol 记日志跳过。"""
        panels = {}
        for sym in universe:
            try:
                df = self._gw.get_kline(sym, datalen=datalen)
                if df is not None and not df.empty:
                    panels[sym] = df
            except Exception as err:
                _log.warning("kline fetch failed for %s: %s", sym, err)
        return panels

    def build_panel(self, panels: dict[str, pd.DataFrame],
                    field: str = "close") -> pd.DataFrame:
        """从 {symbol: df} 构建宽表 DataFrame（index=date, columns=symbols）。"""
        series = {}
        for sym, df in panels.items():
            if field in df.columns:
                series[sym] = df[field]
        return pd.DataFrame(series).sort_index()

    def build_returns(self, panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """构建收益率宽表（hfq 口径，close_hfq 优先）。"""
        series = {}
        for sym, df in panels.items():
            col = "close_hfq" if "close_hfq" in df.columns else "close"
            s = pd.to_numeric(df[col], errors="coerce")
            series[sym] = s.pct_change()
        return pd.DataFrame(series).sort_index()

    def build_tradable_mask(self, panels: dict[str, pd.DataFrame],
                            cfg: BacktestConfig) -> pd.DataFrame:
        """构建可交易掩码（铁律 #2/#3）。

        规则：
        - 停牌：volume == 0 或 NaN
        - 涨跌停：close == open 且涨跌幅 ≈ ±10%（简化判定）
        - ST：名称含 ST 时排除（需额外数据，此处仅做量价判定）
        """
        masks = {}
        for sym, df in panels.items():
            vol = pd.to_numeric(df.get("volume", pd.Series(dtype=float)), errors="coerce")
            close = pd.to_numeric(df["close"], errors="coerce")
            prev_close = close.shift(1)
            ret = (close - prev_close) / prev_close

            suspended = vol.isna() | (vol == 0)
            limit_up = (ret >= 0.095) & (close == df["high"])
            limit_down = (ret <= -0.095) & (close == df["low"])

            tradable = ~(suspended | limit_up | limit_down)
            if cfg.exclude_st:
                tradable = tradable & ~self._is_st(df)

            masks[sym] = tradable
        return pd.DataFrame(masks).sort_index()

    @staticmethod
    def _is_st(df: pd.DataFrame) -> pd.Series:
        """ST 标记检测。若 DataFrame 含 name 列则按名称判定；否则全 False。"""
        if "name" in df.columns:
            return df["name"].str.contains(r"ST|退市", case=False, na=False)
        return pd.Series(False, index=df.index)

    def _get_industry_series(self, symbols: list[str],
                             as_of: Optional[str] = None) -> pd.Series:
        """铁律 #5：时点行业分类。返回 pd.Series(index=symbols, values=industry_l2)。"""
        industries = {}
        for sym in symbols:
            cls = get_classification(sym, standard="sw", as_of=as_of)
            industries[sym] = cls.get("industry_l2", "未知") if cls else "未知"
        return pd.Series(industries)

    def _get_mcap_series(self, panels: dict[str, pd.DataFrame]) -> pd.Series:
        """近似市值：volume × close（日成交额代理）。精确市值需额外数据源。"""
        mcaps = {}
        for sym, df in panels.items():
            if "volume" in df.columns and "close" in df.columns:
                vol = pd.to_numeric(df["volume"], errors="coerce")
                close = pd.to_numeric(df["close"], errors="coerce")
                mcaps[sym] = (vol * close).iloc[-1] if len(vol) > 0 else np.nan
            else:
                mcaps[sym] = np.nan
        return pd.Series(mcaps)

    def run_quintile(self, universe: Sequence[str],
                     factor_name: str,
                     datalen: int = 250,
                     cfg: Optional[BacktestConfig] = None,
                     n_groups: int = 5) -> dict:
        """完整分层回测流程。

        1. 拉取 universe 内所有标的 K 线
        2. 构建因子面板
        3. 计算可交易掩码
        4. 执行分层回测（含成本扣除）
        5. 返回 IC 报告 + 分层绩效 + 换手率
        """
        cfg = cfg or BacktestConfig()
        t0 = time.time()

        panels = self.fetch_universe(universe, datalen=datalen)
        if len(panels) < n_groups * 5:
            return {"error": f"universe too small: {len(panels)} < {n_groups * 5}"}

        ret = self.build_returns(panels)
        symbols = list(panels.keys())

        if factor_name not in FACTOR_REGISTRY:
            return {"error": f"unknown factor: {factor_name}"}

        data_for_factor = {
            "close": self.build_panel(panels, "close"),
            "ret": ret,
            "vol": self.build_panel(panels, "volume"),
            "amount": self.build_panel(panels, "volume") * self.build_panel(panels, "close"),
            "bench_ret": ret.mean(axis=1),
            "float_shares": self.build_panel(panels, "volume"),
        }
        factor = FACTOR_REGISTRY[factor_name](data_for_factor)
        factor = factor.shift(cfg.delay_days)

        fwd_ret = ret.shift(-cfg.delay_days)

        tradable = None
        if cfg.exclude_suspended or cfg.exclude_limit_up_down:
            tradable = self.build_tradable_mask(panels, cfg)

        bt = quintile_backtest(factor, fwd_ret, cfg=cfg, n_groups=n_groups,
                               tradable=tradable)

        ics = ic_series(factor, fwd_ret)
        report = factor_report(ics)

        elapsed = time.time() - t0
        return {
            "factor": factor_name,
            "universe_size": len(panels),
            "config": asdict(cfg),
            "ic_report": report,
            "quintile_metrics": {k: v for k, v in bt.items()
                                 if k.startswith("metrics_")},
            "annual_turnover": bt.get("annual_turnover", {}),
            "nav": {k: v.tolist() if hasattr(v, "tolist") else v
                    for k, v in bt.get("nav", {}).items()},
            "elapsed_seconds": round(elapsed, 2),
        }

    def run_factor_ic(self, universe: Sequence[str],
                      factor_name: str,
                      datalen: int = 250) -> dict:
        """仅计算因子 IC 报告（不执行分层回测），用于因子筛选。"""
        panels = self.fetch_universe(universe, datalen=datalen)
        if len(panels) < 10:
            return {"error": f"universe too small: {len(panels)}"}

        ret = self.build_returns(panels)

        if factor_name not in FACTOR_REGISTRY:
            return {"error": f"unknown factor: {factor_name}"}

        data_for_factor = {
            "close": self.build_panel(panels, "close"),
            "ret": ret,
            "vol": self.build_panel(panels, "volume"),
            "amount": self.build_panel(panels, "volume") * self.build_panel(panels, "close"),
            "bench_ret": ret.mean(axis=1),
            "float_shares": self.build_panel(panels, "volume"),
        }
        factor = FACTOR_REGISTRY[factor_name](data_for_factor).shift(1)
        fwd_ret = ret.shift(-1)

        ics = ic_series(factor, fwd_ret)
        return factor_report(ics)
