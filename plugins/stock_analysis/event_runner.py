# event_runner.py — 事件研究编排层：连接 DB 事件 + gateway 行情 → factor_lab.event_study
# P1-W11：支持分红/送转/配股等事件的研究，计算 CAR（累计超额收益）。
from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from .factor_lab import event_study as _event_study_core
from .gateway import DataGateway
from .models_sa import list_corp_actions

_log = logging.getLogger("stock_analysis.event_runner")


class EventRunner:
    """事件研究编排器。

    用法:
        runner = EventRunner()
        result = runner.run_event_study(
            symbols=["CN:600519", "CN:000858"],
            event_type="dividend",
            pre=5, post=20,
        )
    """

    def __init__(self, gateway: Optional[DataGateway] = None):
        self._gw = gateway or DataGateway()

    def _collect_events(self, symbols: Sequence[str],
                        event_type: Optional[str] = None,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None) -> pd.DataFrame:
        """从 sa_corp_action 收集事件，构建 events DataFrame。"""
        rows = []
        for sym in symbols:
            actions = list_corp_actions(sym, start_date=start_date, end_date=end_date)
            for act in actions:
                if event_type and act.get("action_type") != event_type:
                    continue
                rows.append({
                    "uid": sym,
                    "event_date": pd.Timestamp(act["ex_date"]),
                    "action_type": act.get("action_type", ""),
                    "cash_div": float(act.get("cash_div", 0)),
                    "split_ratio": float(act.get("split_ratio", 1)),
                })
        if not rows:
            return pd.DataFrame(columns=["uid", "event_date", "action_type"])
        return pd.DataFrame(rows)

    def _build_return_panel(self, symbols: Sequence[str],
                            datalen: int = 500) -> tuple[pd.DataFrame, pd.Series]:
        """构建收益率宽表 + 基准收益序列。"""
        ret_series = {}
        for sym in symbols:
            try:
                df = self._gw.get_kline(sym, datalen=datalen)
                if df is None or df.empty:
                    continue
                col = "close_hfq" if "close_hfq" in df.columns else "close"
                s = pd.to_numeric(df[col], errors="coerce")
                ret_series[sym] = s.pct_change()
            except Exception as err:
                _log.warning("kline fetch failed for %s: %s", sym, err)

        if not ret_series:
            return pd.DataFrame(), pd.Series(dtype=float)

        ret_df = pd.DataFrame(ret_series).sort_index()
        bench = ret_df.mean(axis=1)
        return ret_df, bench

    def run_event_study(self, symbols: Sequence[str],
                        event_type: Optional[str] = None,
                        pre: int = 5,
                        post: int = 20,
                        datalen: int = 500,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None) -> dict:
        """完整事件研究流程。

        1. 从 DB 收集事件
        2. 拉取相关标的数据
        3. 计算 [-pre, +post] 窗口 CAR
        4. 返回聚合结果
        """
        events = self._collect_events(symbols, event_type=event_type,
                                      start_date=start_date, end_date=end_date)
        if events.empty:
            return {"n_events": 0, "message": "no events found"}

        ret_df, bench = self._build_return_panel(symbols, datalen=datalen)
        if ret_df.empty:
            return {"n_events": 0, "message": "no price data available"}

        result = _event_study_core(ret_df, bench, events[["uid", "event_date"]],
                                   pre=pre, post=post)

        car_by_day = result.get("car_by_day", {})
        result["car_by_day"] = {
            str(k): v for k, v in car_by_day.items()
        } if car_by_day else {}

        result["event_type"] = event_type or "all"
        result["symbols_count"] = len(symbols)
        result["window"] = f"[-{pre}, +{post}]"
        return result

    def run_multi_event_study(self, symbols: Sequence[str],
                              event_types: Sequence[str] = ("dividend", "split"),
                              pre: int = 5,
                              post: int = 20,
                              datalen: int = 500) -> dict[str, dict]:
        """按事件类型分别做事件研究，返回 {event_type: result}。"""
        results = {}
        for et in event_types:
            results[et] = self.run_event_study(
                symbols, event_type=et, pre=pre, post=post, datalen=datalen)
        return results
