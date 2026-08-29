"""Flask routes exposed by the VeroRun stock analysis plugin."""

import functools
import logging
import threading
import time

from flask import Blueprint, jsonify, render_template, request

_LOGGER = logging.getLogger(__name__)

from .stock_skill import StockAnalysisSkill

stock_analysis_bp = Blueprint(
    "stock_analysis",
    __name__,
    url_prefix="/admin/stock-analysis",
    template_folder="templates",
)


def _skill():
    config = None
    try:
        from flask import current_app
        pm = current_app.extensions.get("plugin_manager")
        if pm is not None and pm.is_enabled("stock_analysis"):
            config = pm.get_config("stock_analysis")
    except Exception:
        config = None
    return StockAnalysisSkill(config=config)


def _require_admin():
    from services.jwt_service import validate_token

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("sso_token") or request.headers.get("X-Token")
    payload = validate_token(token) if token else None
    return payload if payload and payload.get("is_admin") else None


# ── 跨 worker 限流（Q4：统一走公共组件 plugins/_base/ratelimit.py） ──
def _rate_limit(key: str, limit: int, window: float) -> bool:
    """PG 窗口计数公共组件；DB 异常降级进程内。"""
    from plugins._base.ratelimit import check_rate_limit
    return check_rate_limit(key, limit=int(limit), window=int(window))


def _admin_required(limit: int = 60, window: float = 60.0):
    """admin 鉴权 + 限流；未登录 401，超限 429。"""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            payload = _require_admin()
            if not payload:
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            key = "stock_analysis:" + str(payload.get("sub") or "unknown")
            if not _rate_limit(key, limit, window):
                return jsonify({"success": False, "error": "Too Many Requests"}), 429
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _symbol():
    """修复 SA-D2：区分缺失/超长/非法字符，返回 (symbol, error)。"""
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return None, "symbol is required"
    if len(symbol) > 12:
        return None, "symbol too long (max 12 characters)"
    if not symbol.replace(".", "").isalnum():
        return None, "symbol contains invalid characters"
    return symbol, None


@stock_analysis_bp.get("/")
def stock_analysis_page():
    if not _require_admin():
        return "", 401
    return render_template("stock_analysis.html")


@stock_analysis_bp.get("/api/analyze")
@_admin_required(limit=30, window=60.0)
def analyze():
    symbol, sym_err = _symbol()
    if symbol is None:
        return jsonify({"success": False, "error": sym_err}), 400
    analysis_type = request.args.get("type", "llm").lower()
    if analysis_type not in {"technical", "fundamental", "sentiment", "llm"}:
        return jsonify({"success": False, "error": "unsupported analysis type"}), 400
    months = request.args.get("months", 6, type=int)
    months = max(1, min(months, 36))
    try:
        result = _skill().analyze(symbol, analysis_type=analysis_type, months=months)
    except Exception as error:
        _LOGGER.error("股票分析失败 symbol=%s type=%s: %s", symbol, analysis_type, error)
        return jsonify({"success": False, "error": "analysis failed, please retry later"}), 500
    return jsonify({"success": not bool(result.error), "result": result.to_json()})


@stock_analysis_bp.get("/api/signal")
@_admin_required(limit=60, window=60.0)
def signal():
    symbol, sym_err = _symbol()
    if symbol is None:
        return jsonify({"success": False, "error": sym_err}), 400
    try:
        signal_value = _skill().get_signal(symbol)
    except Exception as error:
        _LOGGER.error("获取技术信号失败 symbol=%s: %s", symbol, error)
        return jsonify({"success": False, "error": "signal fetch failed"}), 500
    return jsonify({"success": True, "symbol": symbol, "signal": signal_value})


@stock_analysis_bp.get("/api/market")
@_admin_required()
def market():
    try:
        return jsonify({"success": True, "data": _skill().market_overview()})
    except Exception as error:
        _LOGGER.error("获取市场概况失败: %s", error)
        return jsonify({"success": False, "error": "market data unavailable"}), 500


@stock_analysis_bp.get("/api/sectors")
@_admin_required()
def sectors():
    top_n = request.args.get("top_n", 10, type=int)
    data = _skill().sector_ranking(max(1, min(top_n, 100)))
    if isinstance(data, dict) and data.get("error"):
        return jsonify({"success": False, "error": data["error"]}), 501
    return jsonify({"success": True, "data": data})
