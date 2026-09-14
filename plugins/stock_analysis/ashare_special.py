"""
ashare_special.py — A 股特色数据模块（P2 W17-18）

提供 A 股独有的特色数据接口：
  - 龙虎榜（top_list / top_inst）
  - 融资融券（margin）
  - 北向资金（moneyflow_hsgt / hsgt_top10）
  - 限售股解禁（share_float）
  - 股东户数（stk_holdernumber）
  - 股本变动（stk_factor / daily_basic）

数据源：Tushare Pro（需要相应积分）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from .tushare_client import get_pro

_log = logging.getLogger("stock_analysis.ashare_special")


def _safe_call(pro, method: str, **kwargs) -> Optional[pd.DataFrame]:
    """安全调用 Tushare 接口，失败返回 None。"""
    try:
        fn = getattr(pro, method, None)
        if fn is None:
            _log.warning("tushare method %s not available", method)
            return None
        df = fn(**kwargs)
        if df is None or df.empty:
            return None
        return df
    except Exception as err:
        _log.warning("tushare %s failed: %s", method, err)
        return None


# ================================================================== 龙虎榜


def fetch_top_list(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """龙虎榜明细（个股上榜记录）。

    Parameters
    ----------
    symbol : str
        股票代码（如 600519 或 600519.SH）。
    start_date, end_date : str, optional
        日期范围（YYYYMMDD），默认近 30 天。
    """
    pro = get_pro()
    from .providers.tushare_provider import to_ts_code
    ts_code = to_ts_code(symbol)

    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    df = _safe_call(pro, "top_list",
                    ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "trade_date": row.get("trade_date"),
            "reason": row.get("reason"),
            "close": row.get("close"),
            "pct_change": row.get("pct_change"),
            "buy": row.get("buy"),
            "sell": row.get("sell"),
            "net_buy": row.get("net_buy"),
            "amount": row.get("amount"),
        })
    return records


def fetch_top_inst(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """龙虎榜机构买卖明细。"""
    pro = get_pro()
    from .providers.tushare_provider import to_ts_code
    ts_code = to_ts_code(symbol)

    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    df = _safe_call(pro, "top_inst",
                    ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "trade_date": row.get("trade_date"),
            "exalter": row.get("exalter"),
            "side": row.get("side"),
            "exalter_type": row.get("exalter_type"),
            "buy": row.get("buy"),
            "sell": row.get("sell"),
        })
    return records


# ================================================================== 融资融券


def fetch_margin(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """融资融券明细（个股两融数据）。"""
    pro = get_pro()
    from .providers.tushare_provider import to_ts_code
    ts_code = to_ts_code(symbol)

    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    df = _safe_call(pro, "margin",
                    ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "trade_date": row.get("trade_date"),
            "rzye": row.get("rzye"),           # 融资余额
            "rqye": row.get("rqye"),           # 融券余额
            "rzmre": row.get("rzmre"),         # 融资买入额
            "rqml": row.get("rqml"),           # 融券卖出量
            "rzche": row.get("rzche"),         # 融资偿还额
            "rqchl": row.get("rqchl"),         # 融券偿还量
        })
    return records


def fetch_margin_detail(
    symbol: str,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    """融资融券最新快照。"""
    records = fetch_margin(symbol, start_date=trade_date, end_date=trade_date)
    if not records:
        return {}
    latest = records[-1]
    return {
        "trade_date": latest.get("trade_date"),
        "rzye": latest.get("rzye"),
        "rqye": latest.get("rqye"),
        "rz_rq_total": (latest.get("rzye") or 0) + (latest.get("rqye") or 0),
    }


# ================================================================== 北向资金


def fetch_northbound_flow(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """沪深港通资金流向（北向资金汇总）。"""
    pro = get_pro()

    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    df = _safe_call(pro, "moneyflow_hsgt",
                    start_date=start_date, end_date=end_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "trade_date": row.get("trade_date"),
            "north_money": row.get("north_money"),     # 北向资金（净买入，百万）
            "south_money": row.get("south_money"),     # 南向资金
            "ggt_ss": row.get("ggt_ss"),               # 港股通（沪）
            "ggt_sz": row.get("ggt_sz"),               # 港股通（深）
            "hgt": row.get("hgt"),                     # 沪股通
            "sgt": row.get("sgt"),                     # 深股通
        })
    return records


def fetch_northbound_top10(
    trade_date: Optional[str] = None,
    market: str = "sh",
) -> List[Dict[str, Any]]:
    """沪深港通十大成交股。

    Parameters
    ----------
    market : str
        "sh" 沪股通，"sz" 深股通。
    """
    pro = get_pro()

    if not trade_date:
        trade_date = datetime.now().strftime("%Y%m%d")

    method = "hsgt_top10" if market.lower() == "sh" else "sgt_top10"
    df = _safe_call(pro, method, trade_date=trade_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "ts_code": row.get("ts_code"),
            "name": row.get("name"),
            "close": row.get("close"),
            "pct_change": row.get("pct_change"),
            "rank": row.get("rank"),
            "market_type": row.get("market_type"),
            "amount": row.get("amount"),
            "net_amount": row.get("net_amount"),
            "buy": row.get("buy"),
            "sell": row.get("sell"),
        })
    return records


# ================================================================== 限售股解禁


def fetch_share_float(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """限售股解禁明细。"""
    pro = get_pro()
    from .providers.tushare_provider import to_ts_code
    ts_code = to_ts_code(symbol)

    if not end_date:
        end_date = (datetime.now() + timedelta(days=180)).strftime("%Y%m%d")
    if not start_date:
        start_date = datetime.now().strftime("%Y%m%d")

    df = _safe_call(pro, "share_float",
                    ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "ann_date": row.get("ann_date"),
            "float_date": row.get("float_date"),
            "float_share": row.get("float_share"),
            "float_ratio": row.get("float_ratio"),
            "holder_type": row.get("holder_type"),
        })
    return records


# ================================================================== 股东户数


def fetch_holder_number(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """股东户数变化。"""
    pro = get_pro()
    from .providers.tushare_provider import to_ts_code
    ts_code = to_ts_code(symbol)

    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")

    df = _safe_call(pro, "stk_holdernumber",
                    ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df is None:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "ann_date": row.get("ann_date"),
            "end_date": row.get("end_date"),
            "holder_num": row.get("holder_num"),
        })
    return records


# ================================================================== 股本变动


def fetch_share_capital(
    symbol: str,
) -> Dict[str, Any]:
    """最新股本结构。"""
    pro = get_pro()
    from .providers.tushare_provider import to_ts_code
    ts_code = to_ts_code(symbol)

    df = _safe_call(pro, "daily_basic", ts_code=ts_code,
                    fields="ts_code,trade_date,total_share,float_share,total_mv")
    if df is None:
        return {}

    latest = df.sort_values("trade_date", ascending=False).iloc[0]
    return {
        "trade_date": latest.get("trade_date"),
        "total_share": latest.get("total_share"),
        "float_share": latest.get("float_share"),
        "total_mv": latest.get("total_mv"),
    }


# ================================================================== 聚合接口


def fetch_all_special(
    symbol: str,
    days: int = 30,
) -> Dict[str, Any]:
    """一次性获取个股全部 A 股特色数据。

    每个类别独立 try/except，单类失败不影响其他。
    """
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    result: Dict[str, Any] = {"symbol": symbol, "as_of": end_date}

    try:
        result["top_list"] = fetch_top_list(symbol, start_date, end_date)
    except Exception as err:
        _log.warning("top_list failed for %s: %s", symbol, err)
        result["top_list"] = []

    try:
        result["margin"] = fetch_margin(symbol, start_date, end_date)
        result["margin_latest"] = fetch_margin_detail(symbol)
    except Exception as err:
        _log.warning("margin failed for %s: %s", symbol, err)
        result["margin"] = []
        result["margin_latest"] = {}

    try:
        result["share_float"] = fetch_share_float(symbol, start_date, end_date)
    except Exception as err:
        _log.warning("share_float failed for %s: %s", symbol, err)
        result["share_float"] = []

    try:
        result["holder_number"] = fetch_holder_number(symbol, start_date, end_date)
    except Exception as err:
        _log.warning("holder_number failed for %s: %s", symbol, err)
        result["holder_number"] = []

    try:
        result["share_capital"] = fetch_share_capital(symbol)
    except Exception as err:
        _log.warning("share_capital failed for %s: %s", symbol, err)
        result["share_capital"] = {}

    return result
