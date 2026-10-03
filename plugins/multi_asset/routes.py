"""Flask routes exposed by the multi_asset plugin.

Auth model mirrors the stock_analysis baseline (review V-08): every endpoint
requires a valid JWT with the plugin permission (or admin); missing token -> 401,
insufficient permission -> 403, rate exceeded -> 429. Responses use the platform
contract ``{ok, data, error, meta}``.
"""
import functools
import logging
import os
import sys

from flask import Blueprint, current_app, jsonify, render_template, request

_LOGGER = logging.getLogger(__name__)

# The admin app puts the repo root and auth-center on sys.path; do it defensively so
# this module also imports cleanly under a bare pytest run.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _path in (_ROOT, os.path.join(_ROOT, "auth-center")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from . import adapters, asset_symbol as sym, data_link, models  # noqa: E402

multi_asset_bp = Blueprint(
    "multi_asset",
    __name__,
    url_prefix="/admin/multi-asset",
    template_folder="templates",
)

READ_PERM = "multi_asset.read"
WRITE_PERM = "multi_asset.write"

_VALID_FREQS = ("daily", "weekly", "monthly", "60m", "30m", "15m", "5m", "1m")
_MAX_SYMBOL_LEN = 24


# ── auth ────────────────────────────────────────────────────────────────────

def _token() -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.args.get("token")
    if not token:
        token = request.cookies.get("sso_token") or request.cookies.get("tm_token")
    return token or ""


def _payload():
    """Validate the JWT; returns (payload, error). None payload -> unauthorized."""
    try:
        from services.jwt_service import validate_token
    except Exception:
        try:
            sys.path.insert(0, os.path.join(_ROOT, "auth-center"))
            from services.jwt_service import validate_token
        except Exception:
            return None, "auth service unavailable"
    token = _token()
    return (validate_token(token) if token else None), None


def _authorize(perm: str):
    """Return (payload, error): None payload -> 401; error text -> 403."""
    payload, err = _payload()
    if payload is None:
        return None, err or "Unauthorized"
    if payload.get("is_admin"):
        return payload, None
    perms = payload.get("permissions") or []
    if perm in perms:
        return payload, None
    return payload, "Forbidden"


def _rate_limit(key: str, limit: int, window: int) -> bool:
    try:
        from plugins._base.ratelimit import check_rate_limit
        return bool(check_rate_limit(key, limit=int(limit), window=int(window)))
    except Exception:
        return True      # fail-open: rate limiting must not take the API down


def _perm_required(perm: str, endpoint: str, limit: int = 60, window: int = 60):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            payload, err = _authorize(perm)
            if payload is None:
                return _error(err or "Unauthorized", 401)
            if err:
                return _error(err, 403)
            key = "multi_asset:%s:%s" % (payload.get("sub") or "unknown", endpoint)
            if not _rate_limit(key, limit, window):
                return _error("Too Many Requests", 429)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ── contract helpers ────────────────────────────────────────────────────────

def _error(message: str, status: int):
    return jsonify({"ok": False, "data": None, "error": message, "meta": None}), status


def _ok(data, meta=None, status: int = 200):
    return jsonify({"ok": True, "data": data, "error": None, "meta": meta}), status


def _plugin():
    try:
        pm = current_app.extensions.get("plugin_manager")
        return pm.get_instance("multi_asset") if pm is not None else None
    except Exception:
        return None


def _t(key: str) -> str:
    plugin = _plugin()
    try:
        return plugin.t(key) if plugin is not None else key
    except Exception:
        return key


def _derivative_meta(asset_type: str, meta: dict = None) -> dict:
    """Compliance: derivatives carry an explicit high-leverage risk disclosure."""
    meta = dict(meta or {})
    if str(asset_type).upper() in ("FUTURE", "OPTION"):
        meta["risk_disclosure"] = _t("Derivative risk notice")
        meta["suitability"] = "professional_only"
    return meta


# ── input validation ────────────────────────────────────────────────────────

def _symbol_arg():
    symbol = (request.args.get("symbol") or "").strip()
    if not symbol:
        return None, "symbol is required"
    if len(symbol) > _MAX_SYMBOL_LEN:
        return None, "symbol too long"
    if not symbol.isascii():
        return None, "symbol must be ASCII"
    return symbol, None


def _asset_type_arg():
    raw = (request.args.get("asset_type") or "").strip().upper()
    if not raw:
        return None, None
    if raw not in sym.AssetType.__members__:
        return None, "unsupported asset type"
    return raw, None


def _freq_arg():
    freq = (request.args.get("freq") or "daily").strip().lower()
    if freq not in _VALID_FREQS:
        return None, "unsupported frequency"
    return freq, None


# ── page ────────────────────────────────────────────────────────────────────

@multi_asset_bp.get("/")
def page():
    payload, err = _authorize(READ_PERM)
    if payload is None:
        return _error(err or "Unauthorized", 401)
    if err:
        return _error(err, 403)
    labels = {key: _t(key) for key in (
        "Multi-Asset Workbench", "Symbol", "Asset Type", "Frequency", "Load Bars",
        "Quote", "Profile", "Instrument Search", "Search", "Storage", "Source",
        "Rows", "Trade Date", "No data.", "Derivative risk notice",
        "Futures", "Options", "Funds", "Bonds", "Equity", "Chain sources")}
    return render_template("multi_asset.html", labels=labels,
                           constants=_constants_payload(),
                           token=request.args.get("token") or "")


# ── api ─────────────────────────────────────────────────────────────────────

def _constants_payload() -> dict:
    return {
        "asset_types": list(sym.AssetType.__members__),
        "exchanges": list(sym.Exchange.__members__),
        "exchange_mic": dict(sym.EXCHANGE_MIC),
        "freqs": list(_VALID_FREQS),
        "chains": {at: adapters.chain_sources(at) for at in adapters.CHAINS},
        "contract_available": adapters.CONTRACT_AVAILABLE,
    }


@multi_asset_bp.get("/api/constants")
@_perm_required(READ_PERM, "constants")
def api_constants():
    return _ok(_constants_payload())


@multi_asset_bp.get("/api/resolve")
@_perm_required(READ_PERM, "resolve", limit=120)
def api_resolve():
    symbol, sym_err = _symbol_arg()
    if symbol is None:
        return _error(sym_err, 400)
    asset_type, at_err = _asset_type_arg()
    if at_err:
        return _error(at_err, 400)
    try:
        return _ok(data_link.resolve(symbol, asset_type=asset_type))
    except ValueError as err:
        return _error(str(err) or "invalid symbol", 400)


@multi_asset_bp.get("/api/bars")
@_perm_required(READ_PERM, "bars", limit=60)
def api_bars():
    symbol, sym_err = _symbol_arg()
    if symbol is None:
        return _error(sym_err, 400)
    asset_type, at_err = _asset_type_arg()
    if at_err:
        return _error(at_err, 400)
    freq, freq_err = _freq_arg()
    if freq_err:
        return _error(freq_err, 400)
    datalen = max(1, min(request.args.get("datalen", 120, type=int) or 120, 2000))
    persist = (request.args.get("persist", "1") not in ("0", "false", "False"))
    try:
        payload = data_link.fetch_bars(symbol, asset_type=asset_type, freq=freq,
                                       datalen=datalen, persist=persist)
    except ValueError as err:
        return _error(str(err) or "invalid symbol", 400)
    except adapters.ProviderUnavailable as err:
        return _ok(None, meta=_derivative_meta(asset_type or "", {
            "source_unavailable": True, "detail": str(err)}), status=503)
    return _ok(payload, meta=_derivative_meta(payload.get("asset_type") or ""))


@multi_asset_bp.get("/api/quote")
@_perm_required(READ_PERM, "quote", limit=120)
def api_quote():
    symbol, sym_err = _symbol_arg()
    if symbol is None:
        return _error(sym_err, 400)
    asset_type, at_err = _asset_type_arg()
    if at_err:
        return _error(at_err, 400)
    try:
        payload = data_link.fetch_quote(symbol, asset_type=asset_type)
    except ValueError as err:
        return _error(str(err) or "invalid symbol", 400)
    except adapters.ProviderUnavailable as err:
        return _ok(None, meta=_derivative_meta(asset_type or "", {
            "source_unavailable": True, "detail": str(err)}), status=503)
    return _ok(payload, meta=_derivative_meta(payload.get("asset_type") or ""))


@multi_asset_bp.get("/api/profile")
@_perm_required(READ_PERM, "profile", limit=60)
def api_profile():
    symbol, sym_err = _symbol_arg()
    if symbol is None:
        return _error(sym_err, 400)
    asset_type, at_err = _asset_type_arg()
    if at_err:
        return _error(at_err, 400)
    try:
        payload = data_link.fetch_profile(symbol, asset_type=asset_type)
    except ValueError as err:
        return _error(str(err) or "invalid symbol", 400)
    except adapters.ProviderUnavailable as err:
        return _ok(None, meta={"source_unavailable": True, "detail": str(err)},
                   status=503)
    return _ok(payload)


@multi_asset_bp.get("/api/search")
@_perm_required(READ_PERM, "search", limit=120)
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return _error("q is required", 400)
    asset_type, at_err = _asset_type_arg()
    if at_err:
        return _error(at_err, 400)
    limit = max(1, min(request.args.get("limit", 10, type=int) or 10, 30))
    items = []
    if asset_type in (None, "FUTURE"):
        items.extend(sym.search_variety(query, limit=limit))
    try:
        items.extend(models.search_ref_instruments(query, asset_type=asset_type,
                                                   limit=limit))
    except Exception as err:
        _LOGGER.warning("ref search unavailable: %s", err)
    return _ok({"items": items[:limit], "total": len(items[:limit])},
               meta={"source": "multi_asset"})


@multi_asset_bp.get("/api/storage")
@_perm_required(READ_PERM, "storage", limit=30)
def api_storage():
    try:
        stats = models.storage_stats()
        logs = models.list_fetch_log(limit=20)
    except Exception as err:
        return _ok(None, meta={"storage_unavailable": True, "detail": str(err)},
                   status=503)
    return _ok({"stats": stats, "recent_fetches": logs})


@multi_asset_bp.get("/api/health")
@_perm_required(READ_PERM, "health", limit=60)
def api_health():
    """Chain + contract + storage status (also registered as a platform health check)."""
    return _ok(health_report())


def health_report() -> dict:
    chains = {at: adapters.chain_sources(at) for at in adapters.CHAINS}
    return {
        "contract_available": adapters.CONTRACT_AVAILABLE,
        "gateway_available": data_link.gateway_available(),
        "chains": chains,
        "chain_ok": all(len(v) >= 2 for v in chains.values()),
        "cooldown": adapters.cooldown_state(),
    }
