"""asset_symbol.py --- Single authoritative asset identification module.

Design contract (aligned to ISO 10962:2021 CFI first-letter semantics and to the
exchange-local code rules of SSE / SZSE / BSE / CFFEX / SHFE / INE / DCE / CZCE / GFEX):

  * Case is a *discriminator*, never discarded. CZCE contract codes are UPPERCASE
    with a 3-digit month and no century digit; SHFE / INE / DCE / GFEX / CFFEX use
    lowercase-with-4-digit (commodity) or uppercase-with-4-digit (financial).
  * Product -> exchange resolution uses an explicit variety map, not a whitelist
    of a handful of names.
  * The 6-digit CN code path is a *fallback* heuristic; when the caller already
    knows the exchange it should pass ``exchange=`` so the exchange segment table
    is applied authoritatively (see ``parse_cn_code``).

Rationale: replace the scattered per-module regexes (secmaster._infer_market etc.)
with one authoritative module. This module does not touch stock_analysis.

Note on Chinese variety names: they live in ``data/futures_varieties.json`` so the
Python sources stay free of hard-coded CJK (i18n gate `--check-cn`), and are loaded
lazily with an English-only fallback.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .asset_data import load_data

__all__ = [
    "AssetType", "Exchange", "EXCHANGE_MIC", "ASSET_TYPE_CFI",
    "ParsedInstrument", "parse_future_symbol", "parse_cn_code", "parse_symbol",
    "contract_month", "contract_expiry", "infer_century", "is_future_symbol",
    "search_variety", "variety_name", "FUTURES_PRODUCTS", "normalize",
]


class AssetType(str, Enum):
    """Asset class; values align with the CFI first letter (ISO 10962:2021)."""
    EQUITY = "EQUITY"   # CFI 'E'
    BOND = "BOND"       # CFI 'D'
    FUND = "FUND"       # CFI 'C'
    FUTURE = "FUTURE"   # CFI 'F'
    OPTION = "OPTION"   # CFI 'O'
    SPOT = "SPOT"       # CFI 'R' (reserved)
    SWAP = "SWAP"       # CFI 'S' (reserved)


# CFI category letter (ISO 10962:2021) for each asset type.
ASSET_TYPE_CFI = {
    AssetType.EQUITY: "E",
    AssetType.BOND: "D",
    AssetType.FUND: "C",
    AssetType.FUTURE: "F",
    AssetType.OPTION: "O",
    AssetType.SPOT: "R",
    AssetType.SWAP: "S",
}


class Exchange(str, Enum):
    SSE = "SSE"       # Shanghai Stock Exchange
    SZSE = "SZSE"     # Shenzhen Stock Exchange
    BSE = "BSE"       # Beijing Stock Exchange
    CFFEX = "CFFEX"   # China Financial Futures Exchange
    SHFE = "SHFE"     # Shanghai Futures Exchange
    INE = "INE"       # Shanghai International Energy Exchange
    DCE = "DCE"       # Dalian Commodity Exchange
    CZCE = "CZCE"     # Zhengzhou Commodity Exchange
    GFEX = "GFEX"     # Guangzhou Futures Exchange


# ISO 10383 MIC. GFEX was assigned on 2025-05-26; the official MIC is XGFE,
# confirmed against the ISO 20022 MIC annex (May 2025 release):
#   "GFEX  CN  GUANGZHOU FUTURES EXCHANGE  GFEX"
# (The earlier design draft used XGEF, which is not the registered code.)
EXCHANGE_MIC = {
    Exchange.SSE.value: "XSHG",
    Exchange.SZSE.value: "XSHE",
    Exchange.BSE.value: "XBSE",
    Exchange.CFFEX.value: "XCFE",
    Exchange.SHFE.value: "XSGE",
    Exchange.INE.value: "XINE",
    Exchange.DCE.value: "XDCE",
    Exchange.CZCE.value: "XZCE",
    Exchange.GFEX.value: "XGFE",
}


# --------------------------------------------------------------------------- #
# Futures variety map: product code -> exchange.                             #
# Uppercase products are CZCE (3-digit months) or CFFEX (4-digit months).    #
# Lowercase products are SHFE / INE / DCE / GFEX (4-digit months).           #
# --------------------------------------------------------------------------- #

# CFFEX financial futures (uppercase product + 4-digit YYMM).
CFFEX_PRODUCTS = frozenset({
    "IF", "IH", "IC", "IM",            # index futures
    "T", "TF", "TS", "TL",             # treasury futures (10Y / 5Y / 2Y / 30Y)
})
# CFFEX index options (uppercase + 4-digit); detected as OPTION, not FUTURE.
CFFEX_OPTION_PRODUCTS = frozenset({"IO", "HO", "MO"})

# Lowercase commodity products -> exchange.
FUTURES_LOWER = {
    # SHFE
    "cu": "SHFE", "al": "SHFE", "zn": "SHFE", "pb": "SHFE", "ni": "SHFE",
    "sn": "SHFE", "au": "SHFE", "ag": "SHFE", "rb": "SHFE", "hc": "SHFE",
    "ss": "SHFE", "bu": "SHFE", "ru": "SHFE", "sp": "SHFE", "fu": "SHFE",
    "ao": "SHFE", "br": "SHFE", "wr": "SHFE",
    # INE (Shanghai International Energy Exchange)
    "sc": "INE", "lu": "INE", "nr": "INE", "bc": "INE", "ec": "INE",
    # DCE
    "a": "DCE", "b": "DCE", "m": "DCE", "y": "DCE", "p": "DCE", "c": "DCE",
    "cs": "DCE", "jd": "DCE", "l": "DCE", "v": "DCE", "pp": "DCE", "j": "DCE",
    "jm": "DCE", "i": "DCE", "eg": "DCE", "eb": "DCE", "pg": "DCE",
    "rr": "DCE", "lh": "DCE", "bb": "DCE", "fb": "DCE", "lg": "DCE",
    # GFEX
    "si": "GFEX", "lc": "GFEX", "ps": "GFEX",
}

# CZCE products (uppercase, 3-digit months without a century digit).
CZCE_PRODUCTS = frozenset({
    "SA", "TA", "MA", "CF", "SR", "AP", "RM", "OI", "FG", "ZC", "SF", "SM",
    "PF", "PK", "UR", "CJ", "PX", "SH", "WH", "PM", "RI", "LR", "JR", "RS",
    "CY", "PR",
})

# All known futures product codes (for the symbol-search layer).
FUTURES_PRODUCTS = frozenset(CFFEX_PRODUCTS | set(FUTURES_LOWER) | CZCE_PRODUCTS)

_CONTRACT_RE = re.compile(r"^(?P<product>[A-Za-z]{1,3})(?P<digits>\d{1,4})$")
_CN_CODE_RE = re.compile(r"^\d{6}$")
_PREFIXED_CN_RE = re.compile(r"^(?P<pfx>sh|sz|bj)[.\-]?(?P<num>\d{6})$", re.IGNORECASE)
_EXCHANGE_OF_PREFIX = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}

# --------------------------------------------------------------------------- #
# Variety names (zh from data/futures_varieties.json, en inline below).       #
# --------------------------------------------------------------------------- #
_VARIETY_EN = {
    "IF": "CSI 300 Index Future", "IH": "SSE 50 Index Future",
    "IC": "CSI 500 Index Future", "IM": "CSI 1000 Index Future",
    "T": "10Y Treasury Future", "TF": "5Y Treasury Future",
    "TS": "2Y Treasury Future", "TL": "30Y Treasury Future",
    "cu": "Copper", "al": "Aluminium", "zn": "Zinc", "pb": "Lead",
    "ni": "Nickel", "sn": "Tin", "au": "Gold", "ag": "Silver",
    "rb": "Rebar", "hc": "Hot-rolled Coil", "ss": "Stainless Steel",
    "bu": "Bitumen", "ru": "Natural Rubber", "sp": "Wood Pulp",
    "fu": "Fuel Oil", "ao": "Alumina", "br": "Butadiene Rubber",
    "wr": "Wire Rod",
    "sc": "Crude Oil", "lu": "Low Sulfur Fuel Oil", "nr": "TSR 20 Rubber",
    "bc": "International Copper", "ec": "Container Freight Index (Europe)",
    "a": "Soybean No.1", "b": "Soybean No.2", "m": "Soybean Meal",
    "y": "Soybean Oil", "p": "Palm Oil", "c": "Corn", "cs": "Corn Starch",
    "jd": "Egg", "l": "LLDPE", "v": "PVC", "pp": "Polypropylene",
    "j": "Coke", "jm": "Coking Coal", "i": "Iron Ore",
    "eg": "Ethylene Glycol", "eb": "Styrene", "pg": "LPG",
    "rr": "Japonica Rice", "lh": "Live Hog", "bb": "Plywood",
    "fb": "Fiberboard", "lg": "Log",
    "si": "Industrial Silicon", "lc": "Lithium Carbonate", "ps": "Polysilicon",
    "SA": "Soda Ash", "TA": "PTA", "MA": "Methanol", "CF": "Cotton",
    "SR": "White Sugar", "AP": "Apple", "RM": "Rapeseed Meal",
    "OI": "Rapeseed Oil", "FG": "Glass", "ZC": "Thermal Coal",
    "SF": "Ferrosilicon", "SM": "Manganese Silicon", "PF": "Staple Fiber",
    "PK": "Peanut", "UR": "Urea", "CJ": "Red Date", "PX": "Paraxylene",
    "SH": "Caustic Soda", "WH": "Strong Wheat", "PM": "Common Wheat",
    "RI": "Early Rice", "LR": "Late Rice", "JR": "Japonica Rice (CZCE)",
    "RS": "Rapeseed", "CY": "Cotton Yarn", "PR": "Bottle Chip",
}

_VARIETY_ZH: dict = {}
_VARIETY_LOCK = threading.Lock()
_VARIETY_LOADED = False


def _load_variety_zh() -> dict:
    """Lazily load Chinese variety names from data/futures_varieties.json.

    The names live in a JSON asset (not in this module) because the i18n standard
    keeps CJK literals out of ``*.py`` sources; see asset_data.load_data.
    """
    global _VARIETY_LOADED, _VARIETY_ZH
    if _VARIETY_LOADED:
        return _VARIETY_ZH
    with _VARIETY_LOCK:
        if _VARIETY_LOADED:
            return _VARIETY_ZH
        loaded = load_data("futures_varieties.json")
        _VARIETY_ZH = {str(k): str(v) for k, v in loaded.items()} if loaded else {}
        _VARIETY_LOADED = True
    return _VARIETY_ZH


def variety_name(product: str, lang: str = "zh-CN") -> str:
    """Variety display name; falls back to the product code when unknown."""
    p = str(product or "").strip()
    if not p:
        return ""
    if str(lang).lower().startswith("zh"):
        zh = _load_variety_zh().get(p) or _load_variety_zh().get(p.upper())
        if zh:
            return zh
    return _VARIETY_EN.get(p) or _VARIETY_EN.get(p.upper()) or p


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParsedInstrument:
    """Normalized instrument identity: (asset_type, code, exchange)."""
    asset_type: AssetType
    code: str
    exchange: str
    mic: str = ""

    @property
    def key(self) -> str:
        """Canonical storage key: ``ASSET:EXCHANGE:CODE``."""
        return "%s:%s:%s" % (self.asset_type.value, self.exchange, self.code)

    def to_dict(self) -> dict:
        return {"asset_type": self.asset_type.value, "code": self.code,
                "exchange": self.exchange, "mic": self.mic, "key": self.key}


def _mic_of(exchange) -> str:
    return EXCHANGE_MIC.get(str(exchange), "")


def infer_century(ymm_digits: str, ref_year: int = None) -> int:
    """Infer the full year from a CZCE 3-digit YMM contract code.

    CZCE encodes the year as a *single* digit (the last digit of the year), e.g.
    ``SA605`` -> Y=6, MM=05 -> 2026-05. We pick the candidate year whose last
    digit matches and which is closest to ``ref_year`` (ties prefer the future),
    so a code parsed near year end still resolves forward instead of wrapping.
    """
    if ref_year is None:
        ref_year = date.today().year
    if len(str(ymm_digits)) != 3 or not str(ymm_digits).isdigit():
        raise ValueError("CZCE YMM must be 3 digits, got %r" % (ymm_digits,))
    year_digit = int(str(ymm_digits)[0])
    candidates = [y for y in range(ref_year - 12, ref_year + 4) if y % 10 == year_digit]
    if not candidates:
        raise ValueError("cannot infer century for %r" % (ymm_digits,))
    return min(candidates, key=lambda y: (abs(y - ref_year) - (0.5 if y >= ref_year else 0),))


def contract_month(asset_type, exchange, code: str):
    """Return (year, month) for a dated contract, else None.

    Returns ``None`` for continuous / index / main contract codes such as
    ``RB0`` / ``SA0`` / ``IF00`` which carry no delivery month.
    """
    m = _CONTRACT_RE.match(str(code).strip())
    if not m:
        return None
    digits = m.group("digits")
    if len(digits) < 3:
        return None
    month = int(digits[-2:])
    if not 1 <= month <= 12:
        raise ValueError("illegal contract month %r in %r" % (digits[-2:], code))
    year = 2000 + int(digits[:2]) if len(digits) == 4 else infer_century(digits)
    return year, month


def contract_expiry(asset_type, exchange, code: str):
    """Approximate last-trading / delivery date for a futures contract.

    Display/bucketing only; the authoritative expiry must come from an exchange
    calendar feed:

      * CFFEX index futures     -> 3rd Friday of the delivery month
      * CFFEX treasury futures  -> 2nd Friday of the delivery month
      * commodity futures       -> 15th of the delivery month (mid-month placeholder)
    """
    ym = contract_month(asset_type, exchange, code)
    if ym is None:
        return None
    year, month = ym
    product = _CONTRACT_RE.match(str(code).strip())
    product = product.group("product").upper() if product else ""
    if str(exchange) == Exchange.CFFEX.value:
        return _nth_weekday(year, month, 4, 2 if product in {"T", "TF", "TS", "TL"} else 3)
    return date(year, month, 15)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + 7 * (n - 1))


def parse_future_symbol(raw: str):
    """Parse a Chinese futures/options contract code.

    Returns ``(AssetType, exchange, canonical_code)``. Discriminates by *case* and
    *digit width* rather than a whitelist:

      * uppercase + 4 digits -> CFFEX        (IF2603, T2603)
      * uppercase + 3 digits -> CZCE         (SA605, TA605)
      * lowercase + 4 digits -> FUTURES_LOWER (rb2605 SHFE, m2609 DCE, si2606 GFEX)
      * <= 2 digits          -> continuous / main contract (rb0, SA0, IF00)

    Case is the primary discriminator, but it is not used to *reject* real vendor
    feeds: the CFFEX sets are matched first, then CZCE (3-digit only), and only then
    does an uppercase code fall back to the lowercase product map. That fallback is
    required in practice because akshare / Sina main-contract lists are uppercase
    (``RB0``, ``RB2610``) while the exchanges' own canonical form for SHFE / INE /
    DCE / GFEX is lowercase. The three sets are disjoint, so the fallback cannot
    steal a CFFEX or CZCE code (e.g. ``TA2605`` stays an error rather than being
    silently reclassified). The canonical code is therefore normalised: uppercase
    for CFFEX / CZCE, lowercase for the commodity exchanges.
    """
    s = str(raw).strip()
    m = _CONTRACT_RE.match(s)
    if not m:
        raise ValueError("unrecognized futures symbol: %r" % (raw,))
    product = m.group("product")
    digits = m.group("digits")

    if len(digits) >= 3 and not 1 <= int(digits[-2:]) <= 12:
        raise ValueError("illegal contract month in %r" % (raw,))

    if len(digits) == 4:
        if product.isupper():
            if product in CFFEX_PRODUCTS:
                return AssetType.FUTURE, Exchange.CFFEX.value, s.upper()
            if product in CFFEX_OPTION_PRODUCTS:
                return AssetType.OPTION, Exchange.CFFEX.value, s.upper()
        ex = FUTURES_LOWER.get(product.lower())
        if ex is None:
            raise ValueError("unknown 4-digit futures product: %r" % (raw,))
        return AssetType.FUTURE, ex, s.lower()

    if len(digits) == 3:
        # CZCE is the only exchange using 3-digit months, and only in uppercase.
        if not product.isupper():
            raise ValueError("3-digit month contract must be uppercase (CZCE): %r" % (raw,))
        if product in CZCE_PRODUCTS:
            return AssetType.FUTURE, Exchange.CZCE.value, s.upper()
        raise ValueError("unknown CZCE product: %r" % (raw,))

    # len(digits) <= 2 -> continuous / index / main contract (no delivery month).
    if product.isupper() and product in CFFEX_PRODUCTS:
        return AssetType.FUTURE, Exchange.CFFEX.value, s.upper()
    if product.isupper() and product in CZCE_PRODUCTS:
        return AssetType.FUTURE, Exchange.CZCE.value, s.upper()
    ex = FUTURES_LOWER.get(product.lower())
    if ex is None:
        raise ValueError("unknown continuous contract product: %r" % (raw,))
    return AssetType.FUTURE, ex, s.lower()


def is_future_symbol(raw: str) -> bool:
    """True when ``raw`` parses as a futures/options contract code."""
    try:
        parse_future_symbol(raw)
        return True
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# 6-digit CN code segments (SSE 2025 allocation guide + SZSE range table).    #
# Longest prefix wins. 3-digit prefixes disambiguate the SSE/SZSE overlap on  #
# 1xxxxx (e.g. 110/111/113 = SSE convertible, 123/127/128 = SZSE convertible).#
# --------------------------------------------------------------------------- #
_CN_SEGMENTS = (
    # Beijing Stock Exchange
    ("43", AssetType.EQUITY, "BSE"),
    ("83", AssetType.EQUITY, "BSE"),
    ("87", AssetType.EQUITY, "BSE"),
    ("88", AssetType.EQUITY, "BSE"),
    ("92", AssetType.EQUITY, "BSE"),
    # B shares
    ("900", AssetType.EQUITY, "SSE"),
    ("200", AssetType.EQUITY, "SZSE"),
    # SSE STAR market
    ("688", AssetType.EQUITY, "SSE"),
    ("689", AssetType.EQUITY, "SSE"),
    # SSE main board
    ("600", AssetType.EQUITY, "SSE"),
    ("601", AssetType.EQUITY, "SSE"),
    ("603", AssetType.EQUITY, "SSE"),
    ("605", AssetType.EQUITY, "SSE"),
    # SZSE main board / ChiNext
    ("000", AssetType.EQUITY, "SZSE"),
    ("001", AssetType.EQUITY, "SZSE"),
    ("002", AssetType.EQUITY, "SZSE"),
    ("003", AssetType.EQUITY, "SZSE"),
    ("300", AssetType.EQUITY, "SZSE"),
    ("301", AssetType.EQUITY, "SZSE"),
    ("302", AssetType.EQUITY, "SZSE"),
    # SSE funds (ETF / LOF / REITs)
    ("501", AssetType.FUND, "SSE"),
    ("502", AssetType.FUND, "SSE"),
    ("505", AssetType.FUND, "SSE"),
    ("506", AssetType.FUND, "SSE"),
    ("508", AssetType.FUND, "SSE"),
    ("510", AssetType.FUND, "SSE"),
    ("511", AssetType.FUND, "SSE"),
    ("512", AssetType.FUND, "SSE"),
    ("513", AssetType.FUND, "SSE"),
    ("515", AssetType.FUND, "SSE"),
    ("516", AssetType.FUND, "SSE"),
    ("517", AssetType.FUND, "SSE"),
    ("518", AssetType.FUND, "SSE"),
    ("560", AssetType.FUND, "SSE"),
    ("561", AssetType.FUND, "SSE"),
    ("562", AssetType.FUND, "SSE"),
    ("563", AssetType.FUND, "SSE"),
    ("588", AssetType.FUND, "SSE"),
    # SZSE funds (ETF 159 / graded 150-151 / LOF 16x / closed-end 18x)
    ("159", AssetType.FUND, "SZSE"),
    ("150", AssetType.FUND, "SZSE"),
    ("151", AssetType.FUND, "SZSE"),
    ("18", AssetType.FUND, "SZSE"),
    ("16", AssetType.FUND, "SZSE"),
    # SSE bonds
    ("019", AssetType.BOND, "SSE"),
    ("018", AssetType.BOND, "SSE"),
    ("010", AssetType.BOND, "SSE"),
    ("020", AssetType.BOND, "SSE"),
    ("110", AssetType.BOND, "SSE"),
    ("111", AssetType.BOND, "SSE"),
    ("113", AssetType.BOND, "SSE"),
    ("100", AssetType.BOND, "SSE"),
    ("120", AssetType.BOND, "SSE"),
    ("122", AssetType.BOND, "SSE"),
    ("124", AssetType.BOND, "SSE"),
    ("132", AssetType.BOND, "SSE"),
    ("133", AssetType.BOND, "SSE"),
    # SZSE bonds
    ("112", AssetType.BOND, "SZSE"),
    ("114", AssetType.BOND, "SZSE"),
    ("117", AssetType.BOND, "SZSE"),
    ("123", AssetType.BOND, "SZSE"),
    ("127", AssetType.BOND, "SZSE"),
    ("128", AssetType.BOND, "SZSE"),
    # coarse-length fallbacks (declared last so specific segments win)
    ("11", AssetType.BOND, "SSE"),
    ("12", AssetType.BOND, "SSE"),
    ("13", AssetType.BOND, "SSE"),
    ("5", AssetType.FUND, "SSE"),
    ("6", AssetType.EQUITY, "SSE"),
    ("9", AssetType.EQUITY, "SSE"),
    ("3", AssetType.EQUITY, "SZSE"),
    ("0", AssetType.EQUITY, "SZSE"),
    ("2", AssetType.EQUITY, "SZSE"),
)
# Longest prefix first; Python's sort is stable, so declaration order is kept
# among prefixes of equal length.
_CN_SEGMENTS_SORTED = tuple(sorted(_CN_SEGMENTS, key=lambda r: -len(r[0])))


def parse_cn_code(code: str, exchange: str = None):
    """Parse a 6-digit CN code -> ``(AssetType, exchange)``.

    ``exchange`` is an optional *authority hint*: when the caller obtained the code
    from an exchange-scoped feed it should pass it, and the code is then validated
    against that exchange's segment table. Without a hint we fall back to the
    longest matching prefix — a heuristic only, per the review's requirement that
    prefixes must never be the authority.

    Limitation: index codes (000001 / 399001) are indistinguishable from stock codes
    by prefix alone; they must be supplied with an explicit asset_type/context.
    """
    s = str(code).strip()
    if not _CN_CODE_RE.match(s):
        raise ValueError("not a 6-digit CN code: %r" % (code,))
    if exchange:
        ex = str(exchange).upper()
        for prefix, asset_type, seg_ex in _CN_SEGMENTS_SORTED:
            if s.startswith(prefix) and seg_ex == ex:
                return asset_type, ex
        raise ValueError("code %r not found in %s segment table" % (code, ex))
    for prefix, asset_type, seg_ex in _CN_SEGMENTS_SORTED:
        if s.startswith(prefix):
            return asset_type, seg_ex
    raise ValueError("unrecognized CN code: %r" % (code,))


def parse_symbol(raw: str, asset_type=None, exchange=None) -> ParsedInstrument:
    """Universal entry: normalize any supported symbol into a ParsedInstrument.

    Resolution order:
      1. explicit ``asset_type`` -> dispatch to that parser (authoritative);
      2. exchange-prefixed CN code (sh600519 / sz159915 / bj830799);
      3. futures contract pattern (letters + digits, case-sensitive);
      4. bare 6-digit CN code.
    Raises ``ValueError`` when nothing matches (never guesses silently).
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("empty symbol")

    if asset_type:
        at = asset_type if isinstance(asset_type, AssetType) else AssetType(asset_type)
        if at in (AssetType.FUTURE, AssetType.OPTION):
            _, ex, code = parse_future_symbol(s)
            return ParsedInstrument(at, code, ex, _mic_of(ex))
        if _CN_CODE_RE.match(s):
            at2, ex = parse_cn_code(s, exchange=exchange)
            return ParsedInstrument(at2, s, ex, _mic_of(ex))
        raise ValueError("asset_type %s requires a 6-digit CN code: %r" % (at.value, raw))

    m = _PREFIXED_CN_RE.match(s)
    if m:
        ex = _EXCHANGE_OF_PREFIX[m.group("pfx").lower()]
        num = m.group("num")
        at, ex_resolved = parse_cn_code(num, exchange=ex)
        return ParsedInstrument(at, num, ex_resolved, _mic_of(ex_resolved))

    if _CONTRACT_RE.match(s):
        at, ex, code = parse_future_symbol(s)
        return ParsedInstrument(at, code, ex, _mic_of(ex))

    if _CN_CODE_RE.match(s):
        at, ex = parse_cn_code(s, exchange=exchange)
        return ParsedInstrument(at, s, ex, _mic_of(ex))

    raise ValueError("unrecognized symbol: %r" % (raw,))


def normalize(raw: str, asset_type=None, exchange=None) -> dict:
    """``parse_symbol`` -> plain dict (convenience for routes / JSON)."""
    return parse_symbol(raw, asset_type=asset_type, exchange=exchange).to_dict()


def search_variety(query: str, limit: int = 10) -> list:
    """Search futures varieties by product code or variety name (zh/en).

    Serves the plugin's own instrument-search endpoint so that e.g. a Chinese
    variety name resolves inside multi_asset without depending on the stock-only
    symbol_master (review P2-6 / V-12, plugin-scoped).
    """
    q = str(query or "").strip()
    if not q:
        return []
    ql = q.lower()
    out = []
    for product in FUTURES_PRODUCTS:
        zh = variety_name(product, "zh-CN")
        en = variety_name(product, "en")
        if (ql == product.lower() or product.lower().startswith(ql)
                or ql in zh.lower() or ql in en.lower()):
            ex = FUTURES_LOWER.get(product)
            if ex is None:
                ex = (Exchange.CFFEX.value if product in CFFEX_PRODUCTS
                      else Exchange.CZCE.value)
            out.append({"product": product, "name": zh, "name_en": en,
                        "asset_type": AssetType.FUTURE.value,
                        "exchange": ex, "mic": _mic_of(ex)})
    out.sort(key=lambda r: (r["product"].lower() != ql, len(r["product"])))
    return out[:max(1, int(limit or 10))]
