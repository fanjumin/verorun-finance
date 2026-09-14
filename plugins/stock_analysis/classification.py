# classification.py — 行业分类采集管线（申万 SW / 中信 CITIC / GICS）
# P0-W5：填充 sa_classification 表，支持 point-in-time 查询（回测铁律 #5）。
# 数据源：tushare index_classify + index_member（主）；akshare 无对应接口不设备源。
from __future__ import annotations

import logging
import time
from datetime import date, datetime

from . import tushare_client as tsc
from .models_sa import get_classification, upsert_classification

_log = logging.getLogger("stock_analysis.classification")

_STANDARDS = {
    "SW2021": "sw",
    "SW2014": "sw14",
    "CITIC": "citic",
}

_DEFAULT_STANDARD = "sw"


def _pro():
    return tsc.get_pro()


def _normalize_symbol(ts_code: str) -> str:
    """tushare ts_code (600519.SH) → secmaster UID (CN:600519)。"""
    code = ts_code.split(".")[0]
    return f"CN:{code}"


def fetch_classification_tree(standard: str = "SW2021") -> list[dict]:
    """拉取行业分类树（L1/L2/L3）。返回 [{index_code, industry_name, level, src}]。"""
    pro = _pro()
    df = pro.index_classify(src=standard)
    if df is None or df.empty:
        _log.warning("index_classify(%s) returned empty", standard)
        return []
    return df.to_dict("records")


def fetch_index_members(index_code: str, standard: str = "SW2021") -> list[dict]:
    """拉取某行业指数成分股列表。返回 [{index_code, ts_code, in_date, out_date}]。"""
    pro = _pro()
    try:
        df = pro.index_member(index_code=index_code)
    except Exception as err:
        _log.warning("index_member(%s) failed: %s", index_code, err)
        return []
    if df is None or df.empty:
        return []
    return df.to_dict("records")


def _parse_date(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, date):
        return val.isoformat()
    s = str(val).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10] if s else None


def ingest(standard: str = "SW2021", *, sleep_between: float = 0.12) -> dict:
    """全量采集某标准下的行业分类并写入 sa_classification。

    流程：
    1. 拉分类树 → 按 level 分组
    2. 对每个行业指数拉成分股
    3. 成分股的 in_date 作为 effective_from，out_date 作为 effective_to
    4. 按 (symbol, standard, effective_from) 幂等写入

    返回 {"l1": N, "l2": N, "l3": N, "members": N, "errors": N}。
    """
    label = _STANDARDS.get(standard, standard)
    _log.info("classification ingest start: standard=%s (%s)", standard, label)
    t0 = time.time()

    tree = fetch_classification_tree(standard)
    if not tree:
        return {"l1": 0, "l2": 0, "l3": 0, "members": 0, "errors": 0}

    by_level: dict[str, list[dict]] = {}
    for row in tree:
        lvl = str(row.get("level", ""))
        by_level.setdefault(lvl, []).append(row)

    stats = {"l1": len(by_level.get("L1", [])),
             "l2": len(by_level.get("L2", [])),
             "l3": len(by_level.get("L3", [])),
             "members": 0, "errors": 0}

    all_indices = []
    for lvl in ("L1", "L2", "L3"):
        for node in by_level.get(lvl, []):
            all_indices.append((lvl, node))

    for lvl, node in all_indices:
        index_code = node.get("index_code", "")
        industry_name = node.get("industry_name", "")
        if not index_code:
            continue

        parent_l1 = ""
        parent_l2 = ""
        if lvl == "L2":
            parent_l1 = _find_parent(tree, node, "L1")
        elif lvl == "L3":
            parent_l2 = _find_parent(tree, node, "L2")
            parent_l1 = _find_parent(tree, node, "L1")

        members = fetch_index_members(index_code, standard)
        if not members:
            time.sleep(sleep_between)
            continue

        for m in members:
            ts_code = m.get("ts_code", "")
            if not ts_code:
                continue
            symbol = _normalize_symbol(ts_code)
            in_date = _parse_date(m.get("in_date"))
            out_date = _parse_date(m.get("out_date"))

            if not in_date:
                continue

            l1, l2, l3 = "", "", ""
            if lvl == "L1":
                l1 = industry_name
            elif lvl == "L2":
                l1 = parent_l1
                l2 = industry_name
            elif lvl == "L3":
                l1 = parent_l1
                l2 = parent_l2
                l3 = industry_name

            try:
                upsert_classification(
                    symbol=symbol, standard=label,
                    industry_l1=l1 or None, industry_l2=l2 or None,
                    industry_l3=l3 or None,
                    effective_from=in_date, effective_to=out_date,
                    source="tushare")
                stats["members"] += 1
            except Exception as err:
                stats["errors"] += 1
                _log.warning("upsert_classification failed %s/%s: %s",
                             symbol, index_code, err)

        time.sleep(sleep_between)

    elapsed = time.time() - t0
    _log.info("classification ingest done: %s in %.1fs, stats=%s",
              label, elapsed, stats)
    return stats


def _find_parent(tree: list[dict], node: dict, target_level: str) -> str:
    """通过行业名包含关系或 index_code 前缀匹配找父级。

    申万 L2 index_code 前 4 位与 L1 相同；L3 前 6 位与 L2 相同。
    """
    node_code = node.get("index_code", "")
    node_name = node.get("industry_name", "")

    for candidate in tree:
        if candidate.get("level") != target_level:
            continue
        c_code = candidate.get("index_code", "")
        c_name = candidate.get("industry_name", "")
        if target_level == "L1" and node_code.startswith(c_code[:4]):
            return c_name
        if target_level == "L2" and node_code.startswith(c_code[:6]):
            return c_name
    return ""


def resolve_industry(symbol: str, standard: str = "sw",
                     as_of: str | None = None) -> dict:
    """查询某标的在某时点的行业分类。

    symbol 支持 secmaster UID (CN:000001) 或纯代码 (000001)。
    as_of 为 None 时取最新。
    """
    if not symbol.startswith(("CN:", "HK:", "US:")):
        code = symbol.split(".")[0].lstrip("shszbjSHSZBJ")
        symbol = f"CN:{code}"
    return get_classification(symbol, standard=standard, as_of=as_of)


def batch_resolve(symbols: list[str], standard: str = "sw",
                  as_of: str | None = None) -> dict[str, dict]:
    """批量查询行业分类，返回 {symbol: classification_dict}。"""
    result = {}
    for sym in symbols:
        cls = resolve_industry(sym, standard=standard, as_of=as_of)
        if cls:
            result[sym] = cls
    return result
