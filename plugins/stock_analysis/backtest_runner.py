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


# ── A 股涨跌幅档位表（修复 S2-6：原实现统一 ±9.5%，主板/双创/北交/ST 不分）──
_CN_LIMIT = {"main": 0.10, "star_gem": 0.20, "bse": 0.30, "st_main": 0.05}


def price_limit_band(symbol: str, is_st: bool = False) -> float:
    """返回标的日涨跌幅限制（小数）。支持 '600519.SH' / 'CN:600519' / '600519'。

    规则（A 股现行）：
      - 创业板 300/301/302、科创板 688/689 → 20%（其 ST 亦为 20%）
      - 北交所 43/83/87/88/92           → 30%
      - 主板 600/601/603/605/000/001/002 → ST 5%，否则 10%
    """
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    if code[:3] in ("300", "301", "302") or code[:3] in ("688", "689"):
        return _CN_LIMIT["star_gem"]
    if code[:2] in ("43", "83", "87", "88", "92"):
        return _CN_LIMIT["bse"]
    return _CN_LIMIT["st_main"] if is_st else _CN_LIMIT["main"]


# 因子数据需求声明表：缺数据 → 明确拒绝，而不是返回恒定值/空帧（修复 S1-3）
_FACTOR_REQUIRES = {
    "ln_mcap": ("mcap",),
    "turnover_20": ("float_shares",), "turnover_60": ("float_shares",),
    "ep": ("mcap",), "bp": ("mcap",), "sp": ("mcap",), "cfp": ("mcap",),
    "dividend_yield": ("mcap",),
}


def _ts_code_of(symbol: str) -> str:
    """任意 symbol 形式（600519.SH / CN:600519 / sh600519 / 600519）→ tushare ts_code。

    to_ts_code() 只接受 6 位裸码或 sh/sz/bj 前缀，不认带后缀/带市场前缀的形式，
    故此处先归一化为裸码再转换。
    """
    code = "".join(ch for ch in str(symbol) if ch.isdigit())[-6:]
    try:
        from .providers.tushare_provider import to_ts_code
        return to_ts_code(code).upper()
    except Exception:
        return str(symbol).upper()


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
                            cfg: BacktestConfig) -> tuple[pd.DataFrame, dict]:
        """构建可交易掩码（铁律 #2/#3），并回传"实际是否执行"的证据。

        修复 S0-2：旧实现把 exclude_st 交给 `_is_st(df)`，而 K 线帧无 name 列 →
        该分支恒为 False（ST 剔除静默失效）；min_list_days 更是无任何执行点。
        新实现把每一项的 enforced 如实上报，杜绝"config 声明 = 已执行"的失真回显。
        """
        mask_rows: dict[str, pd.Series] = {}
        st_syms = self._st_symbols(list(panels))            # None = 名单不可得
        listed_days = self._listed_days(list(panels))       # None = 上市日不可得
        st_enforced = bool(cfg.exclude_st and st_syms is not None)
        new_enforced = bool(cfg.min_list_days > 0)

        for sym, df in panels.items():
            vol = pd.to_numeric(df.get("volume", pd.Series(np.nan, index=df.index)),
                                errors="coerce")
            # 涨跌停必须用**不复权价**判定（复权价会破坏相对交易所参考价的比例）
            close = pd.to_numeric(df["close"], errors="coerce")
            ret = (close - close.shift(1)) / close.shift(1)
            band = price_limit_band(sym, is_st=bool(st_syms and sym in st_syms))
            tol = 1e-4

            suspended = vol.isna() | (vol == 0)
            limit_up = (ret >= band - tol) & (close >= df["high"] - tol)
            limit_down = (ret <= -band + tol) & (close <= df["low"] + tol)
            tradable = ~(suspended | limit_up | limit_down)

            if st_enforced and sym in st_syms:
                tradable &= False                          # 整段剔除 ST
            if cfg.min_list_days > 0:
                days = (listed_days or {}).get(sym)
                if days is None:
                    days = int(close.notna().sum())        # 降级口径：帧内可见交易日数
                if days < cfg.min_list_days:
                    tradable &= False

            mask_rows[sym] = tradable

        evidence = {
            "t_plus_1": {"declared": True, "enforced": True,
                         "evidence": "factor.shift(delay_days) + fwd_ret.shift(-delay_days)"},
            "exclude_st": {"declared": bool(cfg.exclude_st), "enforced": st_enforced,
                           "evidence": "ST 名单匹配" if st_enforced
                                       else "ST 名单不可得（K 线帧无 name 列且未接入名单源）"},
            "exclude_new_listing": {"declared": cfg.min_list_days > 0, "enforced": new_enforced,
                                    "evidence": "上市日" if listed_days else "帧内可见交易日数（降级口径）"},
            "exclude_suspended_limit": {"declared": True, "enforced": True,
                                        "evidence": f"volume==0 / |ret|>=band(按板块) (tol={tol})"},
            "real_cost": {"declared": True, "enforced": True,
                          "evidence": f"commission={cfg.commission} stamp={cfg.stamp_tax} "
                                      f"slippage={cfg.slippage_bps}bp"},
            "include_delisted": {"declared": bool(cfg.include_delisted),
                                 "enforced": False,
                                 "evidence": "universe 由调用方提供，当前不含退市样本" if cfg.include_delisted
                                             else "已关闭"},
        }
        return pd.DataFrame(mask_rows).sort_index(), evidence

    # 名单类数据进程级缓存（ST 名称 / 上市日）；None = 尚未取过
    _BASIC_CACHE: Optional[dict[str, dict]] = None

    def _stock_basic(self, symbols: list[str]) -> dict[str, dict]:
        """Tushare stock_basic → {ts_code: {name, list_date}}；不可得返回 {}。

        K 线帧只有量价，没有名称/上市日，铁律 #2（剔 ST/次新）必须另有名单源，
        故在回测入口单独取一次并进程内缓存；取不到就返回 {}，由调用方如实上报
        enforced=False，绝不假装已剔除。
        """
        if BacktestRunner._BASIC_CACHE is None:
            basic: dict[str, dict] = {}
            try:
                from .tushare_client import get_pro
                rows = get_pro().stock_basic(fields="ts_code,name,list_date")
                for _, r in rows.iterrows():
                    basic[str(r["ts_code"]).upper()] = {
                        "name": str(r.get("name") or ""),
                        "list_date": str(r.get("list_date") or ""),
                    }
            except Exception as err:      # 无 token / 无权限 / 网络异常 → 降级（不阻断回测）
                _log.info("stock_basic unavailable, ST/次新剔除将如实标注未生效: %s", err)
            BacktestRunner._BASIC_CACHE = basic
        return BacktestRunner._BASIC_CACHE

    def _st_symbols(self, symbols: list[str]) -> Optional[set]:
        """ST/*ST/退市 名单。名单源不可得 → None（调用方据此上报 enforced=False）。"""
        basic = self._stock_basic(symbols)
        if not basic:
            return None
        out = set()
        for sym in symbols:
            info = basic.get(_ts_code_of(sym))
            name = (info or {}).get("name", "").upper()
            if "ST" in name or "退" in name:
                out.add(sym)
        return out

    def _listed_days(self, symbols: list[str]) -> Optional[dict]:
        """各标的已上市交易日数（营业日近似）。任一上市日不可得 → None（整体降级）。"""
        basic = self._stock_basic(symbols)
        if not basic:
            return None
        today = pd.Timestamp(datetime.now().date())
        out: dict[str, int] = {}
        for sym in symbols:
            info = basic.get(_ts_code_of(sym))
            if not info or not info.get("list_date"):
                return None                       # 口径统一：拿不全就不用精确口径
            try:
                listed = pd.Timestamp(info["list_date"])
            except Exception:
                return None
            out[sym] = int(len(pd.bdate_range(listed, today)))
        return out

    def _get_industry_series(self, symbols: list[str],
                             as_of: Optional[str] = None) -> pd.Series:
        """铁律 #5：时点行业分类。返回 pd.Series(index=symbols, values=industry_l2)。"""
        industries = {}
        for sym in symbols:
            cls = get_classification(sym, standard="sw", as_of=as_of)
            industries[sym] = cls.get("industry_l2", "未知") if cls else "未知"
        return pd.Series(industries)

    def _load_share_base(self, symbols: list[str]) -> tuple[pd.Series, pd.Series]:
        """总市值 / 流通股本（点对点，最新一期）。

        取数链（与网关同哲学：授权源优先、免费源兜底、全失败则明确不可用）：
          1) Tushare daily_basic（total_mv / float_share，按日对齐，最准）
          2) akshare 全市场快照 stock_zh_a_spot_em（含总市值/流通市值，免 key）
          3) 不可得 → 返回两个空 Series，由调用方 fail-closed
        """
        mcap: dict[str, float] = {}
        fshare: dict[str, float] = {}
        try:                                     # 源 1
            from .tushare_client import get_pro
            pro = get_pro()
            for sym in symbols:
                df = pro.daily_basic(ts_code=_ts_code_of(sym),
                                     fields="ts_code,trade_date,total_mv,float_share")
                if df is not None and not df.empty:
                    row = df.sort_values("trade_date").iloc[-1]
                    mcap[sym] = float(row["total_mv"]) * 1e4      # 万元 → 元
                    fshare[sym] = float(row["float_share"]) * 1e4  # 万股 → 股
        except Exception as err:
            _log.info("share base via tushare unavailable: %s", err)

        if not mcap:
            try:                                 # 源 2（免 key 兜底）
                import akshare as ak
                snap = ak.stock_zh_a_spot_em()
                code_map = {s.split(".")[0]: s for s in symbols}
                for _, r in snap.iterrows():
                    sym = code_map.get(str(r.get("代码")))
                    if sym:
                        mcap[sym] = float(r.get("总市值") or np.nan)
                        fshare[sym] = (float(r.get("流通市值") or np.nan)
                                       / max(float(r.get("最新价") or 1), 1e-9))
            except Exception as err:
                _log.warning("share base fallback failed: %s", err)

        idx = list(symbols)
        return (pd.Series({s: mcap.get(s, np.nan) for s in idx}),
                pd.Series({s: fshare.get(s, np.nan) for s in idx}))

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

        need = _FACTOR_REQUIRES.get(factor_name, ())
        mcap_s, fshare_s = self._load_share_base(symbols) if need else (None, None)

        missing = [k for k in need
                   if (k == "mcap" and (mcap_s is None or mcap_s.isna().all()))
                   or (k == "float_shares" and (fshare_s is None or fshare_s.isna().all()))]
        if missing:
            return {"error": f"factor '{factor_name}' requires {missing}; "
                             f"数据源不可用（Tushare daily_basic / akshare 均失败）",
                    "error_code": "DATA_UNAVAILABLE"}

        data_for_factor = {
            "close": self.build_panel(panels, "close"),
            "ret": ret,
            "vol": self.build_panel(panels, "volume"),
            "amount": self.build_panel(panels, "volume") * self.build_panel(panels, "close"),
            "bench_ret": ret.mean(axis=1),
        }
        if mcap_s is not None:
            data_for_factor["mcap"] = mcap_s
        if fshare_s is not None:
            data_for_factor["float_shares"] = fshare_s
        factor = FACTOR_REGISTRY[factor_name](data_for_factor)
        factor = factor.shift(cfg.delay_days)

        fwd_ret = ret.shift(-cfg.delay_days)

        tradable, iron = self.build_tradable_mask(panels, cfg) if (
            cfg.exclude_suspended or cfg.exclude_limit_up_down or cfg.exclude_st) else (None, {})

        bt = quintile_backtest(factor, fwd_ret, cfg=cfg, n_groups=n_groups,
                               tradable=tradable)

        ics = ic_series(factor, fwd_ret)
        report = factor_report(ics)

        elapsed = time.time() - t0
        return {
            "factor": factor_name,
            "universe_size": len(panels),
            "config": asdict(cfg),          # 配置（意图）
            "iron_rules": iron,             # ← 新增：铁律实际执行证据（事实）
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

        symbols = list(panels.keys())
        need = _FACTOR_REQUIRES.get(factor_name, ())
        mcap_s, fshare_s = self._load_share_base(symbols) if need else (None, None)

        missing = [k for k in need
                   if (k == "mcap" and (mcap_s is None or mcap_s.isna().all()))
                   or (k == "float_shares" and (fshare_s is None or fshare_s.isna().all()))]
        if missing:
            return {"error": f"factor '{factor_name}' requires {missing}; "
                             f"数据源不可用（Tushare daily_basic / akshare 均失败）",
                    "error_code": "DATA_UNAVAILABLE"}

        data_for_factor = {
            "close": self.build_panel(panels, "close"),
            "ret": ret,
            "vol": self.build_panel(panels, "volume"),
            "amount": self.build_panel(panels, "volume") * self.build_panel(panels, "close"),
            "bench_ret": ret.mean(axis=1),
        }
        if mcap_s is not None:
            data_for_factor["mcap"] = mcap_s
        if fshare_s is not None:
            data_for_factor["float_shares"] = fshare_s
        factor = FACTOR_REGISTRY[factor_name](data_for_factor).shift(1)
        fwd_ret = ret.shift(-1)

        ics = ic_series(factor, fwd_ret)
        return factor_report(ics)
