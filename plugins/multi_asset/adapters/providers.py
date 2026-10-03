"""Concrete providers for multi_asset.

Three real sources, deliberately layered so the registry can declare a dual-source
failover chain per asset class (audit P2-4 / V-11):

  * ``AkshareProvider``       - akshare public library (primary; free, no SLA)
  * ``SinaFinanceProvider``   - direct HTTP to Sina's futures/option endpoints
  * ``SaGatewayProvider``     - reuse the stock_analysis DataGateway singleton
                                (plugin-standard-v1.8 §12.7 runtime data path)

Everything degrades explicitly: an unsupported (asset, category) combination or a
missing optional dependency raises ``ProviderUnavailable`` so the chain moves on
instead of returning silent empty data.

Frequencies above daily are aggregated locally from the daily series (documented,
never fabricated by relabelling a daily frame as weekly).
"""
from __future__ import annotations

import json
import re

from ..asset_data import load_data
from .base import (AssetProviderBase, DataCategory, ProviderUnavailable,
                   cats, make_result, CONTRACT_AVAILABLE)

try:
    from plugins.stock_analysis.providers.base_v2 import egress_get
except Exception:
    egress_get = None

__all__ = ["AkshareProvider", "SinaFinanceProvider", "SaGatewayProvider",
           "sa_gateway", "normalize_ohlcv"]

# akshare returns vendor column names in Chinese; that mapping lives in a data asset
# (data/akshare_cn.json) so this module stays CJK-free for the i18n gate (see
# asset_data.load_data; the lookup is cached, so calling it per fetch is free).
_FREQ_RESAMPLE = {"weekly": "W-FRI", "monthly": "ME"}
# Frequencies the free chain can actually serve. Anything finer is *not* re-labelled
# from the daily series: the provider reports unavailable so the caller never stores
# a daily bar under an intraday freq key (integrity over coverage).
_NATIVE_FREQS = ("daily", "weekly", "monthly")


_NATIVE_FREQS = ("daily", "weekly", "monthly")


def _cn_col_map() -> dict:
    return load_data("akshare_cn.json").get("col_map") or {}


def _guard_freq(provider, freq) -> str:
    """Reject frequencies the free chain cannot natively serve (no re-labelling)."""
    f = str(freq or "daily").lower()
    if f not in _NATIVE_FREQS:
        raise provider._unavailable(
            "freq %s is not provided by this source (daily/weekly/monthly only)" % f)
    return f


def normalize_ohlcv(df, freq: str = "daily"):
    """Normalize a provider frame to a date-indexed OHLCV frame.

    Accepts Chinese or English column names and either a date column or an existing
    DatetimeIndex. Columns absent from the source are simply not present in the
    result (no fabricated zeros).
    """
    import pandas as pd

    if df is None:
        return None
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)
    if df.empty:
        return df
    col_map = _cn_col_map()
    out = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.dropna(subset=["date"])
        out = out.set_index("date").sort_index()
    else:
        try:
            out.index = pd.to_datetime(out.index, errors="coerce")
            out = out[~out.index.isna()].sort_index()
        except Exception:
            return out
    out.index.name = "date"
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    keep = [c for c in ("open", "high", "low", "close", "volume", "amount",
                        "open_interest") if c in out.columns]
    if keep:
        out = out[keep]
    rule = _FREQ_RESAMPLE.get(str(freq or "daily"))
    if rule:
        agg = {c: ("last" if c == "close" else "max" if c == "high"
                   else "min" if c == "low" else "first" if c == "open"
                   else "sum") for c in out.columns}
        out = out.resample(rule).agg(agg).dropna(how="all")
    return out


def sa_gateway():
    """The stock_analysis DataGateway module singleton (real cross-plugin reuse).

    The gateway exposes get_kline / get_quote / get_fundamental / get_macro / ...
    (verified against plugins/stock_analysis/gateway.py). It raises
    ``ProviderError`` on total chain failure, which we translate to
    ``ProviderUnavailable`` so our own failover moves to the next source.
    """
    try:
        from plugins.stock_analysis.gateway import gateway as gw
        return gw
    except Exception:
        return None


def _http_get(url, params=None, headers=None, timeout: int = 15):
    """Governed egress GET (net_proxy when available, direct otherwise)."""
    if egress_get is not None:
        return egress_get(url, caller="multi_asset", timeout=timeout,
                          headers=headers, params=params)
    import requests
    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(url, params=params, headers=headers, timeout=timeout)
    finally:
        session.close()


class AkshareProvider(AssetProviderBase):
    """akshare-backed provider for futures / options / funds / bonds."""

    name = "akshare"
    # Declared capabilities: the parent fetch() rejects undeclared categories.
    categories = cats("KLINE", "QUOTE", "PROFILE")
    kline_freqs = frozenset({"daily", "weekly", "monthly"})

    def _ak(self):
        try:
            import akshare as ak
        except Exception as err:
            raise self._unavailable("akshare not installed: %s" % err)
        return ak

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if not symbol:
            raise self._unavailable("symbol is required")
        if cat is DataCategory.KLINE:
            data = self._kline(symbol, kw)
        elif cat is DataCategory.QUOTE:
            data = self._quote(symbol)
        elif cat is DataCategory.PROFILE:
            data = self._profile(symbol)
        else:
            raise self._unavailable("unsupported category %s" % getattr(cat, "value", cat))
        return make_result(cat, data, self.name,
                           url="akshare:%s" % self.asset_type.lower(),
                           params={"symbol": symbol})

    # -- KLINE -------------------------------------------------------------- #

    def _kline(self, symbol, kw):
        freq = _guard_freq(self, kw.get("freq"))
        at = self.asset_type
        if at == "FUTURE":
            return self._future_kline(symbol, freq)
        if at == "FUND":
            return self._fund_kline(symbol, freq)
        if at == "BOND":
            return self._bond_kline(symbol, freq)
        if at == "OPTION":
            return self._option_kline(symbol, freq)
        raise self._unavailable("kline not supported for asset_type %s" % at)

    def _future_kline(self, code, freq):
        ak = self._ak()
        fn = getattr(ak, "futures_zh_daily_sina", None)
        if fn is None:
            raise self._unavailable("akshare.futures_zh_daily_sina unavailable")
        df = fn(symbol=str(code).upper())
        return normalize_ohlcv(df, freq)

    def _fund_kline(self, code, freq):
        ak = self._ak()
        period = freq if freq in ("daily", "weekly", "monthly") else "daily"
        s = str(code)
        if s.startswith(("15", "50", "51", "56", "58")):
            fn = getattr(ak, "fund_etf_hist_em", None)
            if fn is None:
                raise self._unavailable("akshare.fund_etf_hist_em unavailable")
            df = fn(symbol=s, period=period, adjust="")
            return normalize_ohlcv(df, "daily")
        if s.startswith(("16", "18")):
            fn = getattr(ak, "fund_lof_hist_em", None)
            if fn is None:
                raise self._unavailable("akshare.fund_lof_hist_em unavailable")
            df = fn(symbol=s, period=period, adjust="")
            return normalize_ohlcv(df, "daily")
        # Open-end fund: NAV series (close <- unit NAV).
        fn = getattr(ak, "fund_open_fund_info_em", None)
        if fn is None:
            raise self._unavailable("akshare.fund_open_fund_info_em unavailable")
        indicator = load_data("akshare_cn.json").get("nav_indicator") or "unit_nav"
        df = fn(symbol=s, indicator=indicator)
        return normalize_ohlcv(df, freq)

    def _bond_kline(self, code, freq):
        ak = self._ak()
        fn = getattr(ak, "bond_zh_hs_daily", None)
        if fn is None:
            raise self._unavailable("akshare.bond_zh_hs_daily unavailable")
        df = fn(symbol=_bond_prefixed(str(code)))
        return normalize_ohlcv(df, freq)

    def _option_kline(self, code, freq):
        ak = self._ak()
        today = kw_date()
        for name in ("option_hist_shfe", "option_hist_dce", "option_hist_czce",
                     "option_hist_gfex"):
            fn = getattr(ak, name, None)
            if fn is None:
                continue
            try:
                df = fn(symbol=_product_of(str(code)), trade_date=today)
            except Exception:
                continue
            if df is not None and not getattr(df, "empty", True):
                return normalize_ohlcv(df, freq)
        raise self._unavailable("no akshare option history endpoint available")

    # -- QUOTE -------------------------------------------------------------- #

    def _quote(self, symbol):
        ak = self._ak()
        at = self.asset_type
        if at == "FUTURE":
            for name in ("futures_zh_realtime", "futures_zh_spot"):
                fn = getattr(ak, name, None)
                if fn is None:
                    continue
                try:
                    df = fn(symbol=str(symbol).upper())
                except Exception:
                    continue
                if df is not None and not getattr(df, "empty", True):
                    return _first_row(df)
            raise self._unavailable("no akshare futures quote endpoint available")
        if at == "FUND":
            for name in ("fund_etf_spot_em", "fund_lof_spot_em"):
                fn = getattr(ak, name, None)
                if fn is None:
                    continue
                try:
                    df = fn()
                except Exception:
                    continue
                row = _row_for_code(df, symbol)
                if row:
                    return row
            raise self._unavailable("no akshare fund quote endpoint available")
        raise self._unavailable("quote not supported for asset_type %s" % at)

    # -- PROFILE ------------------------------------------------------------ #

    def _profile(self, symbol):
        ak = self._ak()
        at = self.asset_type
        if at == "FUTURE":
            fn = getattr(ak, "futures_display_main_sina", None)
            if fn is not None:
                try:
                    row = _row_for_code(fn(), _product_of(str(symbol)))
                    if row:
                        return row
                except Exception:
                    pass
            raise self._unavailable("no futures spec source available")
        if at == "FUND":
            fn = getattr(ak, "fund_etf_spot_em", None)
            if fn is not None:
                try:
                    row = _row_for_code(fn(), symbol)
                    if row:
                        return row
                except Exception:
                    pass
            raise self._unavailable("no fund profile source available")
        if at == "BOND":
            fn = getattr(ak, "bond_zh_hs_cov_spot", None)
            if fn is not None:
                try:
                    row = _row_for_code(fn(), symbol)
                    if row:
                        return row
                except Exception:
                    pass
            raise self._unavailable("no bond profile source available")
        raise self._unavailable("profile not supported for asset_type %s" % at)


class SinaFinanceProvider(AssetProviderBase):
    """Direct HTTP provider for Sina futures / option endpoints (2nd source)."""

    name = "sina"
    # Daily-only endpoint; weekly/monthly are aggregated locally by normalize_ohlcv.
    categories = cats("KLINE")
    kline_freqs = frozenset({"daily", "weekly", "monthly"})
    _FUTURE_KLINE_URL = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
                         "var%20_{sym}=/InnerFuturesNewService.getDailyKLine")

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if not symbol:
            raise self._unavailable("symbol is required")
        if cat is DataCategory.KLINE and self.asset_type in ("FUTURE", "OPTION"):
            return self._kline(symbol, kw)
        raise self._unavailable("unsupported category %s for asset_type %s"
                               % (getattr(cat, "value", cat), self.asset_type))

    def _kline(self, code, kw):
        import pandas as pd
        freq = _guard_freq(self, kw.get("freq"))
        sym_upper = str(code).upper()
        url = self._FUTURE_KLINE_URL.replace("{sym}", sym_upper)
        try:
            resp = _http_get(url, params={"symbol": sym_upper})
            resp.raise_for_status()
            payload = _parse_jsonp(resp.text)
        except Exception as err:
            raise self._unavailable("sina request failed: %s" % err)
        if not payload:
            raise self._unavailable("sina returned no rows for %s" % sym_upper)
        df = pd.DataFrame(payload)
        return normalize_ohlcv(df, freq)


class SaGatewayProvider(AssetProviderBase):
    """Reuse the stock_analysis DataGateway (equity-like 6-digit codes)."""

    name = "sa_gateway"
    # Fallback source: only the equity-style categories the SA gateway can serve.
    categories = cats("KLINE", "QUOTE")
    kline_freqs = frozenset({"daily", "weekly", "monthly"})

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if not symbol:
            raise self._unavailable("symbol is required")
        gw = sa_gateway()
        if gw is None:
            raise self._unavailable("stock_analysis gateway not importable")
        try:
            if cat is DataCategory.KLINE:
                data = gw.get_kline(str(symbol), datalen=int(kw.get("datalen") or 120),
                                    freq=str(kw.get("freq") or "daily"))
            elif cat is DataCategory.QUOTE:
                data = gw.get_quote(str(symbol))
            else:
                raise self._unavailable("unsupported category %s"
                                       % getattr(cat, "value", cat))
        except ProviderUnavailable:
            raise
        except Exception as err:
            raise self._unavailable("gateway fetch failed: %s" % err)
        return make_result(cat, data, self.name,
                           url="stock_analysis.gateway",
                           params={"symbol": symbol, "via": "get_instance"})


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #

def kw_date():
    from datetime import date
    return date.today().strftime("%Y%m%d")


def _bond_prefixed(code: str) -> str:
    """Bond codes need an exchange prefix for akshare (sh/sz)."""
    from .. import asset_symbol as sym
    try:
        _, ex = sym.parse_cn_code(code)
    except Exception:
        return code
    return ("sh" if ex == "SSE" else "sz") + code


def _product_of(symbol: str) -> str:
    m = re.match(r"^([A-Za-z]{1,3})", str(symbol).strip())
    return m.group(1).upper() if m else str(symbol).upper()


def _first_row(df):
    try:
        if df is None or getattr(df, "empty", True):
            return None
        return json.loads(df.head(1).to_json(orient="records"))[0]
    except Exception:
        return None


def _row_for_code(df, code):
    """First row whose values contain ``code`` (case-insensitive), JSON-safe.

    The row is round-tripped through ``to_json`` so numpy scalars never reach the
    Flask JSON encoder (which would raise on them).
    """
    try:
        if df is None or getattr(df, "empty", True):
            return None
        target = str(code).strip().lower()
        for pos, (_, row) in enumerate(df.iterrows()):
            for value in row.values:
                if str(value).strip().lower() == target:
                    return json.loads(df.iloc[[pos]].to_json(orient="records"))[0]
    except Exception:
        return None
    return None


def _parse_jsonp(text: str):
    """Extract the JSON array embedded in a Sina jsonp response."""
    body = str(text or "").strip()
    start = body.find("[")
    end = body.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        return json.loads(body[start:end + 1])
    except Exception:
        return []
