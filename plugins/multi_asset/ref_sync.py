"""ref_sync.py --- refresh reference tables from free public sources (best-effort).

Populates ``ma_future_contracts`` (main contracts) and ``ma_fund_ref`` (ETF list) so
the instrument-search endpoint and futures parsing have real data behind them
(review V-12 plugin scope). Every step degrades silently: a missing optional
dependency or a source outage logs and returns an empty result instead of raising.
"""
from __future__ import annotations

import logging
import re

from . import asset_symbol as sym, models
from .asset_data import load_data
from .events import emit_data_ready

_log = logging.getLogger("multi_asset.ref_sync")

__all__ = ["sync_future_contracts", "sync_fund_refs", "sync_all"]

# Vendor column candidates. Chinese names live in data/akshare_cn.json, not here:
# the i18n gate forbids CJK literals in *.py sources (load_data is cached).
def _name_cols():
    return tuple(load_data("akshare_cn.json").get("fund_name_cols") or ("name",))


def _code_cols():
    return tuple(load_data("akshare_cn.json").get("fund_code_cols")
                 or ("code", "symbol"))


def _contract_cols():
    return tuple(load_data("akshare_cn.json").get("contract_code_cols")
                 or ("symbol", "code"))


def _ak():
    try:
        import akshare as ak
        return ak
    except Exception as err:
        _log.info("akshare unavailable for ref sync: %s", err)
        return None


def _pick(row: dict, candidates) -> str:
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return ""


def sync_future_contracts() -> dict:
    """Upsert main futures contracts (continuous symbols such as RB0 / SA0)."""
    ak = _ak()
    if ak is None:
        return {"rows": 0, "skipped": "akshare_unavailable"}
    fn = getattr(ak, "futures_display_main_sina", None)
    if fn is None:
        return {"rows": 0, "skipped": "source_unavailable"}
    try:
        frame = fn()
    except Exception as err:
        _log.info("futures_display_main_sina failed: %s", err)
        return {"rows": 0, "skipped": "source_error"}
    if frame is None or getattr(frame, "empty", True):
        return {"rows": 0, "skipped": "empty"}

    rows = []
    for record in frame.to_dict(orient="records"):
        raw = _pick(record, _contract_cols())
        if not raw:
            continue
        try:
            asset_type, exchange, code = sym.parse_future_symbol(raw)
        except ValueError:
            continue
        if asset_type is not sym.AssetType.FUTURE:
            continue
        product = re.match(r"^([A-Za-z]{1,3})", code)
        rows.append({
            "symbol": code, "exchange": exchange,
            "product": (product.group(1).upper() if product else ""),
            "delivery_year": None, "delivery_month": None,
            "multiplier": None, "tick_size": None,
            "last_trade_date": None, "is_main": 1,
            "continuous_of": None, "source": "akshare_main_sina",
        })
    if not rows:
        return {"rows": 0, "skipped": "no_parsable_rows"}
    written = models.upsert_future_contracts(rows)
    emit_data_ready({"asset_types": ["FUTURE"], "rows": written, "scope": "ref_sync"})
    return {"rows": written}


def sync_fund_refs() -> dict:
    """Upsert the ETF list into ma_fund_ref (name/type), with parsed exchange."""
    ak = _ak()
    if ak is None:
        return {"rows": 0, "skipped": "akshare_unavailable"}
    fn = getattr(ak, "fund_etf_spot_em", None)
    if fn is None:
        return {"rows": 0, "skipped": "source_unavailable"}
    try:
        frame = fn()
    except Exception as err:
        _log.info("fund_etf_spot_em failed: %s", err)
        return {"rows": 0, "skipped": "source_error"}
    if frame is None or getattr(frame, "empty", True):
        return {"rows": 0, "skipped": "empty"}

    rows = []
    for record in frame.to_dict(orient="records"):
        code = _pick(record, _code_cols())
        name = _pick(record, _name_cols())
        if not code or not re.match(r"^\d{6}$", code):
            continue
        try:
            asset_type, exchange = sym.parse_cn_code(code)
        except ValueError:
            continue
        if asset_type is not sym.AssetType.FUND:
            continue
        rows.append({"code": code, "exchange": exchange, "name": name,
                     "name_norm": name.upper(), "fund_type": "ETF",
                     "source": "akshare_etf_spot"})
    if not rows:
        return {"rows": 0, "skipped": "no_parsable_rows"}
    written = models.upsert_fund_ref(rows)
    emit_data_ready({"asset_types": ["FUND"], "rows": written, "scope": "ref_sync"})
    return {"rows": written}


def sync_all() -> dict:
    return {"futures": sync_future_contracts(), "funds": sync_fund_refs()}
