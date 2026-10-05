"""Tushare commercial provider for multi_asset (futures / options / bonds).

Why a separate module instead of extending ``providers.py``: the free chain
(akshare / sina) refuses intraday frequencies on purpose (``_NATIVE_FREQS`` in
providers.py), while the commercial Tushare source can serve them for users whose
token carries the futures / options minute entitlement. Keeping the commercial
source in its own module preserves that integrity guard for the free sources and
keeps token handling in one place.

Token handling is delegated to the stock_analysis client (``tushare_client``):
environment ``TUSHARE_TOKEN`` > the stock_analysis settings page > ``config.yaml``.
No token / no entitlement / interface missing therefore surfaces as
``ProviderUnavailable`` and the per-asset chain silently falls back to akshare and
then sina -- never fabricated data.

Boundary (per the data-completion plan, P1): this round wires the *provider* only.
Greeks are **not** computed or zero-filled here (no commercial greeks source is
wired yet); option-chain rows are exposed for the P2 persistence layer, which owns
the writes into ``ma_option_contracts``.

Bonds (data-completion plan, P2): the same provider serves exchange bonds through
``cb_daily`` (convertible bonds, daily-only; weekly / monthly aggregated locally).
Bond reference rows and the ``ma_bond_ref`` writes belong to the P2 persistence
layer, not to this provider.
"""
from __future__ import annotations

import json
import re

from .base import (AssetProviderBase, DataCategory, ProviderUnavailable,
                   cats, make_result)
from .providers import normalize_ohlcv

__all__ = ["TushareMaProvider"]

# Tushare ``ts_code`` exchange suffix, keyed by our MIC-style exchange code.
_TS_SUFFIX = {
    "SHFE": "SHF",
    "INE": "INE",
    "DCE": "DCE",
    "CZCE": "CZC",
    "CFFEX": "CFX",
    "GFEX": "GFE",
}

# Our intraday freq -> the tushare minute frequency argument.
_MINUTE_FREQ = {"1m": "1min", "5m": "5min", "15m": "15min",
                "30m": "30min", "60m": "60min"}

# Candidate endpoints, newest naming first. Probed at call time so a token or
# package version without one of them degrades gracefully instead of crashing.
_MINUTE_METHODS = {"FUTURE": ("fut_mins", "futures_mins"),
                   "OPTION": ("opt_mins", "option_mins")}
_DAILY_METHODS = {"FUTURE": ("fut_daily",),
                  "OPTION": ("opt_daily",)}

# Bond venues carry the plain exchange suffix (``.SH`` / ``.SZ``), unlike the
# futures venues above.
_BOND_SUFFIX = {"SSE": "SH", "SZSE": "SZ"}
# Exchange bonds are daily-only; weekly / monthly are aggregated by normalize_ohlcv.
_BOND_FREQS = ("daily", "weekly", "monthly")
# Candidate convertible-bond endpoints, probed at call time like the futures ones.
_BOND_DAILY_METHODS = ("cb_daily",)
# Reverse of _BOND_SUFFIX: tushare venue suffix -> our MIC-style exchange code.
_BOND_VENUE = {suffix: venue for venue, suffix in _BOND_SUFFIX.items()}
# Candidate ``cb_basic`` columns per ma_bond_ref target field. Vendor column names
# drift between tushare versions, so each target probes a candidate list and stays
# empty when none is present -- the mapping never guesses a value.
_BOND_REF_COLS = {
    "ts_code":       ("ts_code",),
    "name":          ("bond_short_name", "bond_full_name"),
    "bond_type":     ("cb_type",),
    "coupon_rate":   ("coupon_rate",),
    "issue_date":    ("value_date", "list_date"),
    "maturity_date": ("maturity_date",),
    "credit_rating": ("newest_rating", "issue_rating"),
    "convert_price": ("conv_price", "first_conv_price"),
}

_CONTRACT_RE = re.compile(r"^([A-Za-z]{1,3})(\d+)$")


def _json_rows(df, limit: int = 500) -> list:
    """JSON-safe records (so numpy scalars never reach the Flask encoder)."""
    try:
        if df is None or getattr(df, "empty", True):
            return []
        return json.loads(df.head(int(limit)).to_json(orient="records"))
    except Exception:
        return []


def _to_ohlcv_frame(df):
    """Rename tushare columns onto the canonical OHLCV names normalize expects."""
    if df is None:
        return df
    try:
        if "trade_time" in df.columns:
            df = df.rename(columns={"trade_time": "date"})
            if "trade_date" in df.columns:
                df = df.drop(columns=["trade_date"])
        elif "trade_date" in df.columns:
            df = df.rename(columns={"trade_date": "date"})
        rename = {}
        if "vol" in df.columns and "volume" not in df.columns:
            rename["vol"] = "volume"
        if "oi" in df.columns and "open_interest" not in df.columns:
            rename["oi"] = "open_interest"
        if rename:
            df = df.rename(columns=rename)
    except Exception:
        pass
    return df


def _records(df) -> list:
    """DataFrame -> plain records; empty / unavailable frames collapse to ``[]``."""
    try:
        if df is None or getattr(df, "empty", True):
            return []
        return df.to_dict(orient="records")
    except Exception:
        return []


def _pick(record, candidates):
    """First present, non-empty, non-NaN value among the candidate columns."""
    for key in candidates:
        if key not in record:
            continue
        value = record[key]
        if value is None:
            continue
        if isinstance(value, float) and value != value:      # NaN
            continue
        if str(value).strip() in ("", "nan", "None"):
            continue
        return value
    return None


def _num(record, candidates):
    value = _pick(record, candidates)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value):
    """tushare ``YYYYMMDD`` string -> ISO date string (None-safe)."""
    text = str(value).strip() if value is not None else ""
    if len(text) == 8 and text.isdigit():
        return "%s-%s-%s" % (text[:4], text[4:6], text[6:])
    return text or None


def _bond_ref_rows(records) -> list:
    """Map ``cb_basic`` records onto ``ma_bond_ref`` rows.

    Pure and network-free so it can be unit-tested. Bonds without a resolvable
    venue suffix are skipped; missing columns stay ``None`` (never invented).
    ``cb_basic`` carries no issuer, so ``issuer`` is left empty on purpose.
    """
    out = []
    for record in records or []:
        ts_code = _pick(record, _BOND_REF_COLS["ts_code"])
        if not ts_code or "." not in str(ts_code):
            continue
        code, _, venue = str(ts_code).partition(".")
        exchange = _BOND_VENUE.get(venue.upper())
        if not exchange:
            continue
        name = _pick(record, _BOND_REF_COLS["name"])
        name = str(name).strip() if name is not None else None
        out.append({
            "code": code, "exchange": exchange,
            "name": name or None,
            "name_norm": (name.upper() if name else None),
            "bond_type": _pick(record, _BOND_REF_COLS["bond_type"]),
            "coupon_rate": _num(record, _BOND_REF_COLS["coupon_rate"]),
            "issue_date": _date(_pick(record, _BOND_REF_COLS["issue_date"])),
            "maturity_date": _date(_pick(record, _BOND_REF_COLS["maturity_date"])),
            "issuer": None,
            "credit_rating": _pick(record, _BOND_REF_COLS["credit_rating"]),
            "convert_price": _num(record, _BOND_REF_COLS["convert_price"]),
            "source": "tushare_cb_basic",
        })
    return out


class TushareMaProvider(AssetProviderBase):
    """Commercial Tushare source (futures / options / bonds), intraday-capable.

    Sits at the head of the FUTURE / OPTION / BOND chains: tried first, and when the
    user has not configured a token (or lacks the entitlement) it raises
    ``ProviderUnavailable`` so the chain falls through to the free sources.
    """

    name = "tushare"
    categories = cats("KLINE")
    # Unlike the free chain this source can serve intraday; providers._guard_freq
    # is deliberately NOT used here (it would reject every minute frequency).
    kline_freqs = frozenset({"1m", "5m", "15m", "30m", "60m",
                             "daily", "weekly", "monthly"})

    # -- token / pro_api ---------------------------------------------------- #

    def _pro(self):
        try:
            from plugins.stock_analysis.tushare_client import get_pro
        except Exception as err:
            raise self._unavailable("stock_analysis tushare client unavailable: %s" % err)
        try:
            return get_pro()
        except Exception as err:
            raise self._unavailable("tushare token not configured or invalid: %s" % err)

    # -- contract surface --------------------------------------------------- #

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is not DataCategory.KLINE:
            raise self._unavailable("unsupported category %s"
                                    % getattr(cat, "value", cat))
        if not symbol:
            raise self._unavailable("symbol is required")
        if self.asset_type not in ("FUTURE", "OPTION", "BOND"):
            raise self._unavailable("asset_type %s is not served by tushare"
                                    % self.asset_type)
        freq = str(kw.get("freq") or "daily").lower()
        ts_code = self._ts_code(symbol)
        if self.asset_type == "BOND":
            # Exchange bonds are daily-only (cb_daily). The free chain has the same
            # limit, so intraday is refused rather than re-labelled from daily bars.
            if freq not in _BOND_FREQS:
                raise self._unavailable(
                    "freq %s is not served for bonds (daily/weekly/monthly)" % freq)
            df = self._bond_daily(ts_code, kw)
        elif freq in _MINUTE_FREQ:
            df = self._minute(ts_code, freq, kw)
        else:
            df = self._daily(ts_code, kw)
        data = normalize_ohlcv(_to_ohlcv_frame(df), freq)
        if data is None or getattr(data, "empty", True):
            raise self._unavailable("tushare returned no rows for %s" % ts_code)
        return make_result(cat, data, self.name,
                           url="tushare:%s" % self.asset_type.lower(),
                           params={"symbol": str(symbol), "freq": freq,
                                   "ts_code": ts_code})

    # -- option chain (consumed by the P2 persistence layer) ---------------- #

    def option_chain(self, **filters) -> list:
        """Reference option contracts (``opt_basic``) as JSON-safe rows.

        Returns ``[]`` when empty and raises ``ProviderUnavailable`` when the
        endpoint is missing or the token lacks the entitlement. No Greeks are
        computed here.
        """
        pro = self._pro()
        fn = getattr(pro, "opt_basic", None)
        if fn is None:
            raise self._unavailable("tushare opt_basic unavailable")
        try:
            df = fn(**filters)
        except Exception as err:
            raise self._unavailable("opt_basic failed: %s" % err)
        return _json_rows(df)

    # -- bond reference (consumed by the reference-sync layer) -------------- #

    def bond_reference(self, **filters) -> list:
        """Reference bonds (``cb_basic``) mapped onto ``ma_bond_ref`` rows.

        Convertible bonds only (CB / EB) -- the same subset the free chain serves.
        Returns ``[]`` when nothing matches and raises ``ProviderUnavailable`` when
        the endpoint is missing or the token lacks the entitlement.
        """
        pro = self._pro()
        fn = getattr(pro, "cb_basic", None)
        if fn is None:
            raise self._unavailable("tushare cb_basic unavailable")
        try:
            df = fn(**filters)
        except Exception as err:
            raise self._unavailable("cb_basic failed: %s" % err)
        return _bond_ref_rows(_records(df))

    # -- internals ---------------------------------------------------------- #

    def _ts_code(self, symbol) -> str:
        if self.asset_type == "BOND":
            return self._bond_ts_code(symbol)
        from .. import asset_symbol as sym
        try:
            _at, ex, code = sym.parse_future_symbol(str(symbol))
        except Exception:
            raise self._unavailable("cannot resolve ts_code for %r" % (symbol,))
        suffix = _TS_SUFFIX.get(str(ex))
        if not suffix:
            raise self._unavailable("exchange %s has no tushare suffix" % ex)
        m = _CONTRACT_RE.match(str(code))
        product = (m.group(1) if m else str(code)).upper()
        digits = m.group(2) if m else ""
        base = (product + digits) if len(digits) >= 3 else product
        return "%s.%s" % (base, suffix)

    def _bond_ts_code(self, symbol) -> str:
        """6-digit exchange bond code -> tushare ``ts_code`` (``.SH`` / ``.SZ``)."""
        from .. import asset_symbol as sym
        code = str(symbol).strip()
        try:
            _at, ex = sym.parse_cn_code(code)
        except Exception:
            raise self._unavailable("cannot resolve bond ts_code for %r" % (symbol,))
        suffix = _BOND_SUFFIX.get(str(ex))
        if not suffix:
            raise self._unavailable("exchange %s has no tushare bond suffix" % ex)
        return "%s.%s" % (code, suffix)

    def _minute(self, ts_code, freq, kw):
        params = {"ts_code": ts_code, "freq": _MINUTE_FREQ[freq]}
        return self._call(_MINUTE_METHODS.get(self.asset_type, ()),
                          self._with_dates(params, kw), ts_code)

    def _daily(self, ts_code, kw):
        return self._call(_DAILY_METHODS.get(self.asset_type, ()),
                          self._with_dates({"ts_code": ts_code}, kw), ts_code)

    def _bond_daily(self, ts_code, kw):
        return self._call(_BOND_DAILY_METHODS,
                          self._with_dates({"ts_code": ts_code}, kw), ts_code)

    @staticmethod
    def _with_dates(params: dict, kw) -> dict:
        for key in ("start_date", "end_date"):
            value = kw.get(key)
            if value:
                params[key] = value
        return params

    def _call(self, method_names, params, ts_code):
        """Call the first available endpoint; degrade when none is reachable."""
        pro = self._pro()
        last = None
        for name in method_names:
            fn = getattr(pro, name, None)
            if fn is None:
                continue
            try:
                return fn(**params)
            except TypeError as err:
                # Older signature without the optional date window.
                last = err
                minimal = {"ts_code": ts_code}
                if "freq" in params:
                    minimal["freq"] = params["freq"]
                try:
                    return fn(**minimal)
                except Exception as err2:      # noqa: BLE001 -- entitlement/limit
                    last = err2
                    continue
            except Exception as err:      # noqa: BLE001 -- entitlement/limit
                last = err
                continue
        raise self._unavailable("no tushare endpoint available (%s): %s"
                                % (",".join(method_names) or "none", last))
