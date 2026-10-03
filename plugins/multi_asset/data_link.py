"""data_link.py --- orchestration: resolve symbol -> fetch via chain -> persist.

Single entry point used by the routes layer. Keeps the three responsibilities the
review cares about separate and explicit:

  1. symbol normalization  -> asset_symbol (single authoritative module)
  2. data acquisition      -> adapters chain (real providers, cooldown failover)
  3. persistence + audit   -> models (ma_bars / reference tables / ma_fetch_log)

Bar attribution follows trade_calendar: daily/weekly/monthly bars keep the exchange
trade date carried by the source index; intraday bars are attributed through
``assign_trade_date`` (night session -> next trading day), per audit V-14.
"""
from __future__ import annotations

import logging

from . import adapters, asset_symbol as sym, models, trade_calendar

_log = logging.getLogger("multi_asset.data_link")

__all__ = ["resolve", "fetch_bars", "fetch_quote", "fetch_profile",
           "chain_sources", "gateway_available", "SOURCE_ORDER"]

SOURCE_ORDER = {at: adapters.chain_sources(at) for at in adapters.CHAINS}


def resolve(raw: str, asset_type: str = None, exchange: str = None) -> dict:
    """Normalize a symbol into {asset_type, code, exchange, mic, key}."""
    return sym.normalize(raw, asset_type=asset_type, exchange=exchange)


def gateway_available() -> bool:
    """Whether the stock_analysis DataGateway is importable (cross-plugin reuse)."""
    return adapters.providers.sa_gateway() is not None


def _intraday(freq: str) -> bool:
    return str(freq or "daily") not in ("daily", "weekly", "monthly")


def _bars_to_rows(asset_type: str, code: str, exchange: str, freq: str, frame,
                  source: str):
    """Convert an OHLCV DataFrame into ma_bars row dicts (attribution applied)."""
    rows = []
    for idx, record in frame.iterrows():
        try:
            stamp = idx.to_pydatetime()
        except Exception:
            continue
        if _intraday(freq):
            trade_date = trade_calendar.assign_trade_date(asset_type, exchange, code, stamp)
            bar_time = stamp.strftime("%H:%M")
        else:
            trade_date = stamp.date()
            bar_time = "00:00"
        rows.append({
            "asset_type": asset_type, "symbol": code, "exchange": exchange,
            "freq": freq, "trade_date": trade_date, "bar_time": bar_time,
            "open": record.get("open"), "high": record.get("high"),
            "low": record.get("low"), "close": record.get("close"),
            "volume": record.get("volume"), "amount": record.get("amount"),
            "value": record.get("close") if asset_type in ("FUND", "BOND") else None,
            "source": source,
        })
    return rows


def fetch_bars(symbol: str, asset_type: str = None, exchange: str = None,
               freq: str = "daily", datalen: int = 120, persist: bool = True) -> dict:
    """Fetch a bar series, optionally persisting it into ``ma_bars``.

    Returns a provenance-enriched dict:
      {ok, asset_type, code, exchange, freq, rows, written, bars, source,
       provenance_id, as_of, warnings, trade_date_from, trade_date_to}

    ``rows`` is the row *count*; ``bars`` is the serialized series itself (oldest
    first) so the embedded page can render it without a second round trip.
    """
    inst = sym.parse_symbol(symbol, asset_type=asset_type, exchange=exchange)
    at, code, ex = inst.asset_type.value, inst.code, inst.exchange
    warnings = []
    if not models_ready():
        warnings.append("storage_unavailable")
    try:
        result = adapters.fetch(at, adapters.DataCategory.KLINE, code,
                                freq=freq, datalen=int(datalen or 120))
    except adapters.ProviderUnavailable as err:
        _log.warning("bars fetch failed %s/%s: %s", at, code, err)
        _safe_record(at, code, ex, freq, None, ok=False, warning=str(err))
        raise
    frame = _frame_of(result)
    source = getattr(result, "source", None) or "unknown"
    provenance = getattr(result, "provenance_id", "") or ""
    as_of = getattr(result, "as_of", "") or ""
    warnings.extend(getattr(result, "warnings", None) or [])
    rows = _bars_to_rows(at, code, ex, freq, frame, source) if frame is not None else []
    written = 0
    if persist and rows:
        try:
            written = models.upsert_bars(rows)
        except Exception as err:      # persistence is best-effort, fetch already succeeded
            warnings.append("persist_failed")
            _log.warning("bars persist failed %s/%s: %s", at, code, err)
    _safe_record(at, code, ex, freq, result, ok=True)
    return {
        "ok": True, "asset_type": at, "code": code, "exchange": ex, "freq": freq,
        "rows": len(rows), "written": written, "bars": _serialize(rows),
        "source": source, "provenance_id": provenance, "as_of": as_of,
        "warnings": warnings,
        "trade_date_from": _iso(rows[0]["trade_date"]) if rows else None,
        "trade_date_to": _iso(rows[-1]["trade_date"]) if rows else None,
    }


def fetch_quote(symbol: str, asset_type: str = None, exchange: str = None) -> dict:
    inst = sym.parse_symbol(symbol, asset_type=asset_type, exchange=exchange)
    try:
        result = adapters.fetch(inst.asset_type.value, adapters.DataCategory.QUOTE,
                                inst.code)
    except adapters.ProviderUnavailable as err:
        _safe_record(inst.asset_type.value, inst.code, inst.exchange, None, None,
                     ok=False, warning=str(err))
        raise
    return {"ok": True, "asset_type": inst.asset_type.value, "code": inst.code,
            "exchange": inst.exchange, "source": getattr(result, "source", ""),
            "provenance_id": getattr(result, "provenance_id", ""),
            "as_of": getattr(result, "as_of", ""),
            "data": _frame_of(result) if _is_frame(result) else _payload_of(result)}


def fetch_profile(symbol: str, asset_type: str = None, exchange: str = None) -> dict:
    inst = sym.parse_symbol(symbol, asset_type=asset_type, exchange=exchange)
    result = adapters.fetch(inst.asset_type.value, adapters.DataCategory.PROFILE,
                            inst.code)
    return {"ok": True, "asset_type": inst.asset_type.value, "code": inst.code,
            "exchange": inst.exchange, "source": getattr(result, "source", ""),
            "data": _payload_of(result)}


def chain_sources(asset_type: str) -> list:
    return adapters.chain_sources(asset_type)


# --------------------------------------------------------------------------- #
# internals                                                                   #
# --------------------------------------------------------------------------- #

def _iso(value):
    """ISO string for a date/datetime (None-safe) so the payload is plain JSON."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _serialize(rows) -> list:
    """JSON-safe view of the bar rows (dates/times as ISO strings)."""
    out = []
    for row in rows or []:
        item = dict(row)
        for key in ("trade_date", "bar_time"):
            value = item.get(key)
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        for key, value in list(item.items()):
            if value is not None and hasattr(value, "item"):   # numpy scalar
                item[key] = value.item()
        out.append(item)
    return out


def models_ready() -> bool:
    """Best-effort storage readiness probe (never raises)."""
    try:
        models.ensure_tables()
        return True
    except Exception:
        return False


def _is_frame(result) -> bool:
    try:
        import pandas as pd
        return isinstance(getattr(result, "data", None), pd.DataFrame)
    except Exception:
        return False


def _frame_of(result):
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    try:
        import pandas as pd
        if isinstance(data, pd.DataFrame):
            return data
    except Exception:
        pass
    return None


def _payload_of(result):
    """JSON-safe payload (DataFrame -> records; numpy scalars normalized)."""
    import json

    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if hasattr(data, "to_json"):
        try:
            return json.loads(data.to_json(orient="records", date_format="iso"))
        except Exception:
            pass
    if hasattr(data, "to_dict"):
        try:
            return data.to_dict(orient="records")
        except Exception:
            return data
    return data


def _safe_record(asset_type, code, exchange, freq, result, ok=True, warning=None):
    """Write a fetch-log row; never let audit logging break the main path."""
    try:
        models.record_fetch(
            asset_type, code, exchange=exchange, freq=freq,
            source=(getattr(result, "source", None) if result is not None else None),
            provenance_id=(getattr(result, "provenance_id", None)
                           if result is not None else None),
            as_of=(getattr(result, "as_of", None) if result is not None else None),
            ok=ok, warning=warning)
    except Exception:
        pass
