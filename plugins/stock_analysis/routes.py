"""Flask routes exposed by the VeroRun stock analysis plugin."""

import functools
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import time

from flask import (Blueprint, Response, current_app, jsonify,
                   render_template, request, stream_with_context)

_LOGGER = logging.getLogger(__name__)

from .stock_skill import StockAnalysisSkill
# kb 模块级仅 import json/logging/uuid（无外部依赖、无循环导入风险）；
# 这里提前取到降级异常类型，供 /api/kb/* 在宽 except 之前优先捕获 → 503。
from .kb import KnowledgeBaseUnavailable

stock_analysis_bp = Blueprint(
    "stock_analysis",
    __name__,
    url_prefix="/admin/stock-analysis",
    template_folder="templates",
)

# 桌面端自选分组标签（VeroRun 金融版：由插件声明，壳层只消费、不硬编码）。
# 随 /api/watchlist 响应返回，供前端分组下拉/过滤使用。
WATCHLIST_GROUPS = ["核心持仓", "重点关注", "ETF 观察"]

# 分析/自选类型枚举（唯一来源：原先在 /api/analyze:172 与 /api/watchlist POST:308 各写一份
# 字面量集合，现收敛为一处；models_sa 表列默认值为 'technical'，见 models_sa.py:45）。
# 由 /api/alerts/schema 下发，壳层不再各自镜像。
_ANALYSIS_KINDS = ("technical", "fundamental", "sentiment", "llm")


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
    """返回 (payload, error)。

    payload is None → 未登录 / 无效 token（调用方回 401）；
    error 非空（payload 有效但非管理员）→ 越权（调用方回 403）。
    #SA-AUTHZ-01：区分 401（未登录）与 403（越权），对齐 TC-SA-AUTH-04。
    """
    from services.jwt_service import validate_token

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("sso_token") or request.headers.get("X-Token")
    payload = validate_token(token) if token else None
    if payload is None:
        return None, "Unauthorized"
    if not payload.get("is_admin"):
        return payload, "Forbidden"
    return payload, None


def _require_perm(perm: str):
    """返回 (payload, error)，按权限点校验（层级：admin > write > read）。

    与 _require_admin 区别：不再要求 is_admin，改查 JWT payload.permissions 列表。
    is_admin 用户自动拥有全部权限（向下兼容）。
    """
    from services.jwt_service import validate_token

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("sso_token") or request.headers.get("X-Token")
    payload = validate_token(token) if token else None
    if payload is None:
        return None, "Unauthorized"
    if payload.get("is_admin"):
        return payload, None
    perms = payload.get("permissions") or []
    if perm in perms:
        return payload, None
    if perm == "stock.read" and "stock.write" in perms:
        return payload, None
    if perm == "stock.read" and "stock.admin" in perms:
        return payload, None
    if perm == "stock.write" and "stock.admin" in perms:
        return payload, None
    return payload, "Forbidden"


def _perm_required(perm: str, endpoint: str, limit: int = 60, window: float = 60.0):
    """细粒度权限鉴权 + 限流；未登录 401，越权 403，超限 429。"""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            payload, auth_err = _require_perm(perm)
            if payload is None:
                return jsonify({"success": False, "error": auth_err}), 401
            if auth_err:
                return jsonify({"success": False, "error": auth_err}), 403
            key = "stock_analysis:" + str(payload.get("sub") or "unknown") + ":" + endpoint
            if not _rate_limit(key, limit, window):
                return jsonify({"success": False, "error": "Too Many Requests"}), 429
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ── 跨 worker 限流（Q4：统一走公共组件 plugins/_base/ratelimit.py） ──
def _rate_limit(key: str, limit: int, window: float) -> bool:
    """PG 窗口计数公共组件；DB 异常降级进程内。"""
    from plugins._base.ratelimit import check_rate_limit
    return check_rate_limit(key, limit=int(limit), window=int(window))


def _admin_required(endpoint: str, limit: int = 60, window: float = 60.0):
    """admin 鉴权 + 限流；未登录 401，越权 403，超限 429。

    #SA-BUG-01：限流 key 纳入端点维度（stock_analysis:{sub}:{endpoint}），
    各端点独立计数，避免高频端点填满共享桶误伤其他端点。
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            payload, auth_err = _require_admin()
            if payload is None:
                return jsonify({"success": False, "error": auth_err}), 401
            if auth_err:
                return jsonify({"success": False, "error": auth_err}), 403
            key = "stock_analysis:" + str(payload.get("sub") or "unknown") + ":" + endpoint
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
    # F4：isalnum 接受 Unicode 字母（如"茅台"），必须限定 ASCII
    if not symbol.isascii() or not symbol.replace(".", "").isalnum():
        return None, "symbol contains invalid characters"
    return symbol, None


@stock_analysis_bp.get("/")
def stock_analysis_page():
    payload, auth_err = _require_perm("stock.read")
    if payload is None:
        return "", 401
    if auth_err:
        return "", 403
    return render_template("stock_analysis.html")


@stock_analysis_bp.get("/api/analyze")
@_perm_required("stock.read", "analyze", limit=30, window=60.0)
def analyze():
    symbol, sym_err = _symbol()
    if symbol is None:
        return jsonify({"success": False, "error": sym_err}), 400
    analysis_type = request.args.get("type", "llm").lower()
    if analysis_type not in _ANALYSIS_KINDS:
        return jsonify({"success": False, "error": "unsupported analysis type"}), 400
    months = request.args.get("months", 6, type=int)
    months = max(1, min(months, 36))
    try:
        result = _skill().analyze(symbol, analysis_type=analysis_type, months=months)
    except Exception as error:
        _LOGGER.error("股票分析失败 symbol=%s type=%s: %s", symbol, analysis_type, error)
        return jsonify({"success": False, "error": "analysis failed, please retry later"}), 500
    if not result.error:
        try:
            _sa().record_signal(symbol, analysis_type, result.to_json())
        except Exception:
            pass   # 落库失败不影响分析响应
        # B2/D-4b：结论沉淀统一由调用方（本路由 / jobs_queue）负责，失败仅留痕不阻断
        try:
            from .kb_publish import publish_analysis_kb
            publish_analysis_kb(symbol, analysis_type, result.to_json())
        except Exception as err:
            _LOGGER.warning("kb publish failed %s: %s", symbol, err)
    return jsonify({"success": not bool(result.error), "result": result.to_json()})


@stock_analysis_bp.get("/api/signal")
@_perm_required("stock.read", "signal", limit=60, window=60.0)
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
@_perm_required("stock.read", "market")
def market():
    try:
        return jsonify({"success": True, "data": _skill().market_overview()})
    except Exception as error:
        _LOGGER.error("获取市场概况失败: %s", error)
        return jsonify({"success": False, "error": "market data unavailable"}), 500


@stock_analysis_bp.get("/api/search")
@_perm_required("stock.read", "symbol_search", limit=120, window=60.0)
def symbol_search():
    """标的检索：按代码前缀 / 名称检索。

    此前插件只有"按完整代码查行情"，中文名称在 symbol 校验即被拒（400），
    故新增本接口补上检索层；主数据未就绪时 meta.warming=True，前端据此提示。
    """
    query = (request.args.get("q") or "").strip()
    if not query:
        return _contract_error("q is required", 400)
    if len(query) > 32:
        query = query[:32]
    limit = request.args.get("limit", 10, type=int) or 10
    limit = max(1, min(limit, 30))
    from . import symbol_master
    try:
        items, state = symbol_master.search(query, limit=limit)
    except Exception as error:
        _LOGGER.error("标的检索失败 q=%s: %s", query, error)
        return _contract_error("search unavailable", 503)
    return jsonify({
        "ok": True,
        "data": {"items": items, "total": len(items)},
        "error": None,
        "meta": {
            "source": "symbol_master",
            "ready": bool(state.get("ready")),
            "rows": state.get("rows") or 0,
            "warming": bool(state.get("warming")),
            "detail": state.get("error"),
        },
    })


# ── 阶段 4：标的池 / 批量运行 / 结果检索 ──

def _body_symbol():
    """POST body 版 symbol 校验（GET 用 _symbol）。"""
    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return None, "symbol is required"
    if len(symbol) > 12:
        return None, "symbol too long (max 12 characters)"
    # F4：isalnum 接受 Unicode 字母（如"茅台"），必须限定 ASCII
    if not symbol.isascii() or not symbol.replace(".", "").isalnum():
        return None, "symbol contains invalid characters"
    return symbol, None


def _sa():
    """延迟导入 models_sa 并做建表兜底（#SA-20260831-05：watchlist/results/export
    不依赖 batch.run 隐式初始化，全新部署/清库后首次访问不再 500）。"""
    from . import models_sa as sa_module
    try:
        sa_module.ensure_tables()
    except Exception:
        pass
    return sa_module


@stock_analysis_bp.get("/api/watchlist")
@_perm_required("stock.read", "watchlist_list")
def watchlist_list():
    sa = _sa()
    try:
        with sa.get_db() as conn:
            rows = conn.execute(
                "SELECT symbol, alias, kind, enabled, note, "
                "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
                "FROM sa_watchlist ORDER BY id DESC").fetchall()
        # data 保持原列表结构不变；groups 为插件声明的分组标签（桌面端 UI 消费）
        return jsonify({"success": True, "data": rows, "groups": WATCHLIST_GROUPS})
    except Exception as error:
        _LOGGER.error("watchlist list failed: %s", error)
        return jsonify({"success": False, "error": "watchlist unavailable"}), 500


@stock_analysis_bp.post("/api/watchlist")
@_perm_required("stock.write", "watchlist_add", limit=20, window=60.0)
def watchlist_add():
    sa = _sa()
    symbol, sym_err = _body_symbol()
    if symbol is None:
        return jsonify({"success": False, "error": sym_err}), 400
    body = request.get_json(silent=True) or {}
    alias = (body.get("alias") or "")[:64]
    kind = (body.get("kind") or "technical").lower()
    if kind not in _ANALYSIS_KINDS:
        kind = "technical"
    note = (body.get("note") or "")[:1000]
    try:
        with sa.get_db() as conn:
            row = conn.execute(
                "INSERT INTO sa_watchlist (symbol, alias, kind, note) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (symbol) DO NOTHING RETURNING symbol",
                (symbol, alias, kind, note)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "symbol already exists"}), 409
        return jsonify({"success": True, "symbol": symbol})
    except Exception as error:
        _LOGGER.error("watchlist add failed symbol=%s: %s", symbol, error)
        return jsonify({"success": False, "error": "watchlist add failed"}), 500


@stock_analysis_bp.delete("/api/watchlist")
@_perm_required("stock.write", "watchlist_delete")
def watchlist_delete():
    sa = _sa()
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"success": False, "error": "symbol is required"}), 400
    try:
        with sa.get_db() as conn:
            conn.execute("DELETE FROM sa_watchlist WHERE symbol = ?", (symbol,))
        return jsonify({"success": True})
    except Exception as error:
        _LOGGER.error("watchlist delete failed symbol=%s: %s", symbol, error)
        return jsonify({"success": False, "error": "watchlist delete failed"}), 500


@stock_analysis_bp.post("/api/batch/run")
@_perm_required("stock.write", "batch_run", limit=5, window=60.0)
def batch_run():
    from . import batch
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "technical").lower()
    # P2-4 修复：批量强制 kind∈{technical,fundamental,sentiment}，禁止 llm 批量
    # （无白名单标的池 + 无 LLM 并发信号量时，一次批量会并发放大 LLM 账单）
    if kind not in {"technical", "fundamental", "sentiment"}:
        return jsonify({"success": False, "error": "unsupported analysis type"}), 400
    # #SA-20260831-10：显式传 symbols=[] 不得被 or None 短路成「未传」，
    # 否则空列表绕过非空校验并静默退化为 watchlist 驱动
    symbols = body.get("symbols")
    if symbols is not None:
        if not isinstance(symbols, list) or not symbols:
            return jsonify({"success": False, "error": "symbols must be a non-empty list"}), 400
        symbols = [str(s).strip() for s in symbols if str(s).strip()]
        if not symbols:
            return jsonify({"success": False, "error": "symbols is empty"}), 400
    try:
        result = batch.run_batch(kind=kind, symbols=symbols)
        return jsonify({"success": True, "data": result})
    except Exception as error:
        _LOGGER.error("batch run failed: %s", error)
        return jsonify({"success": False, "error": "batch run failed"}), 500


@stock_analysis_bp.get("/api/batch/results")
@_perm_required("stock.read", "batch_results")
def batch_results():
    sa = _sa()
    run_id = request.args.get("run_id", "").strip()
    symbol = request.args.get("symbol", "").strip()
    signal = request.args.get("signal", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = max(1, min(request.args.get("per_page", 20, type=int), 100))
    clauses, params = [], []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if signal:
        clauses.append("signal = ?")
        params.append(signal)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        with sa.get_db() as conn:
            total = conn.execute(
                "SELECT count(*) AS c FROM sa_analysis_result" + where, params).fetchone()["c"]
            rows = conn.execute(
                "SELECT run_id, symbol, kind, signal, confidence, score, payload, "
                "data_sources, to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
                "FROM sa_analysis_result" + where +
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page]).fetchall()
        return jsonify({"success": True, "total": total, "page": page,
                        "per_page": per_page, "data": rows})
    except Exception as error:
        _LOGGER.error("batch results query failed: %s", error)
        return jsonify({"success": False, "error": "results unavailable"}), 500


@stock_analysis_bp.get("/api/signals/today")
@_perm_required("stock.read", "signals_today", limit=60, window=60.0)
def signals_today():
    """今日信号（sa_signal_log 当日明细，含单股/批量/告警引擎全部来源）。

    背景：总览页「今日信号」此前读 /api/batch/results（sa_analysis_result），而批量分析
    在非交易日直接 skip（周末/节假日），信号实际落到 sa_signal_log —— 卡片在周末恒显示 0，
    与实际信号产出脱节。本端点按「当日真实信号」口径返回，与信号管线闭环对齐。
    """
    sa = _sa()
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    # latest_per_symbol=1 → 每标的一条（当日最新）：「自选与信号」列表要的是
    # 「当前信号」，此前由桌面端自行按 symbol 取最新（自解释时间语义，属口径）。
    # 2026-09-21 整改：口径归插件，用 DISTINCT ON 给出，壳层只渲染。
    per_symbol = request.args.get("latest_per_symbol", "").strip().lower() in ("1", "true", "yes")
    try:
        with sa.get_db() as conn:
            if per_symbol:
                rows = conn.execute(
                    "SELECT DISTINCT ON (symbol) symbol, trade_date, kind, signal, confidence, "
                    "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
                    "FROM sa_signal_log WHERE trade_date = current_date "
                    "ORDER BY symbol, created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT symbol, trade_date, kind, signal, confidence, "
                    "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
                    "FROM sa_signal_log WHERE trade_date = current_date "
                    "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return jsonify({"ok": True, "data": {"signals": [dict(r) for r in rows]},
                        "error": None, "meta": _generated_meta()})
    except Exception as error:
        _LOGGER.error("signals today failed: %s", error)
        return _contract_error("signals today failed", 500)


@stock_analysis_bp.get("/api/signals/daily")
@_perm_required("stock.read", "signals_daily", limit=60, window=60.0)
def signals_daily():
    """按交易日聚合的信号构成（sa_signal_log：每日 买/持/卖 计数）。

    为什么需要这个端点（2026-09-20 总览页「信号构成」柱图）：
      · `/api/signals/today` 只有当日 → 画不出"近 N 个交易日"的时间轴；
      · `/api/batch/results` 读 sa_analysis_result，而批量分析结果现已落
        sa_signal_log —— 实测该接口三页全库 0 行（前端原先用它聚合，图恒空）。
    聚合口径属业务，放插件侧（壳层只消费），前端不再自己 group by。
    返回按 trade_date 升序，前端可直接按序画堆叠柱。
    """
    sa = _sa()
    days = max(2, min(request.args.get("days", 14, type=int), 60))
    try:
        with sa.get_db() as conn:
            rows = conn.execute(
                "SELECT to_char(trade_date, 'YYYY-MM-DD') AS trade_date, "
                "COALESCE(SUM(CASE WHEN signal = 'buy' THEN 1 ELSE 0 END), 0) AS buy, "
                "COALESCE(SUM(CASE WHEN signal = 'sell' THEN 1 ELSE 0 END), 0) AS sell, "
                "COALESCE(SUM(CASE WHEN signal IS NULL OR signal NOT IN ('buy', 'sell') "
                "THEN 1 ELSE 0 END), 0) AS hold "
                "FROM sa_signal_log WHERE trade_date IS NOT NULL "
                "GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?", (days,)).fetchall()
        data = [dict(r) for r in rows]
        data.reverse()
        return jsonify({"ok": True, "data": {"days": data}, "error": None,
                        "meta": _generated_meta()})
    except Exception as error:
        _LOGGER.error("signals daily failed: %s", error)
        return _contract_error("signals daily failed", 500)


@stock_analysis_bp.get("/api/signals/summary")
@_perm_required("stock.read", "signals_summary", limit=30, window=60.0)
def signals_summary():
    """看板聚合：近 N 日信号构成 + 风险雷达 5 维 + 最新 run 的 Top 信号。

    2026-09-21 架构整改：桌面端 StockDashboardPage 此前自行 group by 出日分布、
    自行派生风险雷达 5 维（bull/avgConf/coverage/avgScore/stability）、
    自行解释 run 语义取「最新 run + confidence Top5」—— 聚合与排名口径属业务，
    一律下沉到插件，壳层只渲染（契约 §3：桌面端只渲染不计算）。

    口径说明（与改造前逐字等价，避免用户看到的数字变化）：
      · days  —— sa_signal_log 按交易日聚合，与 /api/signals/daily 同源同口径；
      · radar —— 取最近 limit 条 sa_analysis_result（前端原为三页 batch/results
                 ≈300 行，故 default=300）；coverage 分母为启用中的自选池数量；
      · top   —— 最近一批 run 内 buy/sell 按 confidence 降序取前 5。
    """
    sa = _sa()
    days = max(2, min(request.args.get("days", 14, type=int), 60))
    limit = max(10, min(request.args.get("limit", 300, type=int), 1000))
    try:
        with sa.get_db() as conn:
            day_rows = conn.execute(
                "SELECT to_char(trade_date, 'YYYY-MM-DD') AS trade_date, "
                "COALESCE(SUM(CASE WHEN signal = 'buy' THEN 1 ELSE 0 END), 0) AS buy, "
                "COALESCE(SUM(CASE WHEN signal = 'sell' THEN 1 ELSE 0 END), 0) AS sell, "
                "COALESCE(SUM(CASE WHEN signal IS NULL OR signal NOT IN ('buy', 'sell') "
                "THEN 1 ELSE 0 END), 0) AS hold "
                "FROM sa_signal_log WHERE trade_date IS NOT NULL "
                "GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?", (days,)).fetchall()
            result_rows = [dict(r) for r in conn.execute(
                "SELECT run_id, symbol, signal, confidence, score "
                "FROM sa_analysis_result ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
            wl_total = conn.execute(
                "SELECT COUNT(*) AS n FROM sa_watchlist WHERE enabled = 1").fetchone()["n"]
            result_total = conn.execute(
                "SELECT COUNT(*) AS n FROM sa_analysis_result").fetchone()["n"]
        day_data = [dict(r) for r in day_rows]
        day_data.reverse()

        radar = None
        top: list = []
        total = len(result_rows)
        if total:
            buys = sum(1 for r in result_rows if r["signal"] == "buy")
            conf_sum = sum(float(r["confidence"] or 0) for r in result_rows)
            score_sum = sum(float(r["score"] or 0) for r in result_rows)
            by_symbol: dict = {}
            for r in result_rows:
                by_symbol.setdefault(r["symbol"], set()).add(r["signal"])
            symbols_seen = len(by_symbol)
            consistent = sum(1 for s in by_symbol.values() if len(s) == 1)
            denom = int(wl_total or 0) or symbols_seen
            radar = {
                "bull": round(buys / total * 100),
                "avgConf": round(conf_sum / total * 100),
                "coverage": round(symbols_seen / max(1, denom) * 100),
                "avgScore": round(score_sum / total),
                "stability": round(consistent / (symbols_seen or 1) * 100),
            }
            latest_run = result_rows[0]["run_id"]
            candidates = [r for r in result_rows
                          if r["run_id"] == latest_run and r["signal"] in ("buy", "sell")]
            candidates.sort(key=lambda r: float(r["confidence"] or 0), reverse=True)
            top = [{"symbol": r["symbol"], "signal": r["signal"],
                    "confidence": float(r["confidence"] or 0),
                    "score": r["score"]} for r in candidates[:5]]

        return jsonify({"ok": True,
                        "data": {"days": day_data, "radar": radar, "top": top,
                                 "total": int(result_total or 0),
                                 "window": {"days": days, "limit": limit}},
                        "error": None, "meta": _generated_meta()})
    except Exception as error:
        _LOGGER.error("signals summary failed: %s", error)
        return _contract_error("signals summary failed", 500)


@stock_analysis_bp.get("/api/batch/export")
@_perm_required("stock.read", "batch_export", limit=10, window=60.0)
def batch_export():
    """批量结果导出：JSON 文件下载（全量不分页，可带 run_id/symbol/signal 筛选）。"""
    sa = _sa()
    run_id = request.args.get("run_id", "").strip()
    symbol = request.args.get("symbol", "").strip()
    signal = request.args.get("signal", "").strip()
    clauses, params = [], []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if signal:
        clauses.append("signal = ?")
        params.append(signal)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        with sa.get_db() as conn:
            rows = conn.execute(
                "SELECT run_id, symbol, kind, signal, confidence, score, payload, "
                "data_sources, to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
                "FROM sa_analysis_result" + where + " ORDER BY id DESC",
                params).fetchall()
        data = [dict(r) for r in rows]
        filename = f"batch_{run_id or 'all'}.json"
        # #SA-20260831-11：标准库 json.dumps 无法序列化 Decimal（confidence 等 NUMERIC 列），
        # 复用 Flask JSON provider（Decimal → float），避免导出 500
        resp = Response(current_app.json.dumps(data, ensure_ascii=False, indent=2),
                        mimetype="application/json")
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return resp
    except Exception as error:
        _LOGGER.error("batch export failed: %s", error)
        return jsonify({"success": False, "error": "export unavailable"}), 500


# ── 阶段 2：Tushare 深度估值分位 / 个股资金流 ──

@stock_analysis_bp.get("/api/fundamental-detail")
@_perm_required("stock.read", "fundamental_detail", limit=30, window=60.0)
def fundamental_detail():
    """深财报四表合一（Tushare income/balance/cashflow/fina_indicator）。无数据源返回 404。

    ★ 契约统一（2026-09-17）：本路由原返回 {"success": ...}，与插件统一契约
      {"ok","data","error","meta"} 不一致 —— 前端 unwrap() 只认 ok，照旧接线必然读空。
      HTTP 状态码与 error 文案保持原样，只把外层结构改回统一契约。
    """
    symbol, sym_err = _symbol()
    if symbol is None:
        return jsonify({"ok": False, "data": None, "error": sym_err,
                        "meta": _generated_meta()}), 400
    # periods 透传给 provider（Tushare provider 的 _fetch_fundamental 支持 periods，
    # 网关签名已同步放开）；范围 clamp 到 [1,20]，避免一次拉爆接口配额。
    periods = request.args.get("periods", 8, type=int)
    periods = max(1, min(periods, 20))
    try:
        from .gateway import gateway
        data = gateway.get_fundamental(symbol, periods=periods)
    except Exception as error:
        _LOGGER.warning("fundamental unavailable symbol=%s: %s", symbol, error)
        # ★ 契约统一（2026-09-17）：本路由原先返回 {"success": ...}，与插件统一契约
        #   {"ok","data","error","meta"} 不一致，前端 unwrap() 只认 ok → 接线后必然读空。
        #   现改回统一契约（HTTP 404 与 error 文案保持原样）。
        return jsonify({"ok": False, "data": None,
                        "error": "fundamental unavailable (tushare not configured or no permission)",
                        "meta": _generated_meta()}), 404
    return jsonify({"ok": True, "data": {"symbol": symbol,
                                         "periods": periods,
                                         "tables": data},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/moneyflow")
@_perm_required("stock.read", "moneyflow", limit=30, window=60.0)
def moneyflow():
    """个股资金流（Tushare moneyflow，最近 N 日）。tushare 未配置/无权限时返回 404 优雅降级。"""
    symbol, sym_err = _symbol()
    if symbol is None:
        return jsonify({"success": False, "error": sym_err}), 400
    days = max(1, min(request.args.get("days", 5, type=int), 30))
    try:
        from .gateway import gateway
        frame = gateway.get_moneyflow(symbol, days=days)
        records = json.loads(
            frame.reset_index().to_json(orient="records", date_format="iso"))
    except Exception as error:
        _LOGGER.warning("moneyflow unavailable symbol=%s: %s", symbol, error)
        return jsonify({"success": False,
                        "error": "moneyflow unavailable (tushare not configured or no permission)"}), 404
    return jsonify({"success": True, "symbol": symbol, "days": days, "data": records})


# ── P0-2 信号兑现回算闭环（只读）──

@stock_analysis_bp.get("/api/signal-quality")
@_perm_required("stock.read", "signal_quality")
def signal_quality():
    """只读：按 kind/signal 聚合历史信号命中率与前向收益（数据观察，不构成投资建议）。"""
    days = max(7, min(request.args.get("days", 90, type=int), 365))
    _sa()                            # 建表兜底：冷启动首调不再 500（#SA-20260901-01）
    try:
        from . import signal_quality as sq
        return jsonify({"success": True, "days": days, "data": sq.quality_summary(days)})
    except Exception as error:
        _LOGGER.error("signal quality failed: %s", error)
        return jsonify({"success": False, "error": "signal quality unavailable"}), 500


@stock_analysis_bp.post("/api/signal-realize")
@_perm_required("stock.write", "signal_realize", limit=3, window=60.0)
def signal_realize():
    """手动触发增量回算（限流，防重复刷）。"""
    try:
        from . import signal_quality as sq
        return jsonify({"success": True, "data": sq.realize_signals(days_back=90)})
    except Exception as error:
        _LOGGER.error("signal realize failed: %s", error)
        return jsonify({"success": False, "error": "realize failed"}), 500


# ── 方案B：数据源环境自检 + 一键安装可选依赖（akshare）──
# 用户在管理页确认后才执行 pip 安装；白名单限定，杜绝任意包安装。

def _probe_optional_dep(name: str) -> dict:
    """探测当前解释器能否导入可选依赖，返回 {installed, version}。"""
    try:
        mod = importlib.import_module(name)
        return {"installed": True, "version": getattr(mod, "__version__", None)}
    except ImportError:
        return {"installed": False, "version": None}


@stock_analysis_bp.get("/api/deps/status")
@_perm_required("stock.read", "deps_status")
def deps_status():
    """数据源环境自检：探测可选 Python 依赖（akshare）是否已安装。"""
    return jsonify({"success": True, "data": {"akshare": _probe_optional_dep("akshare")}})


# 允许一键安装的可选依赖白名单（防止任意包安装/命令注入）
_INSTALLABLE = {"akshare": "akshare"}


@stock_analysis_bp.post("/api/deps/install")
@_perm_required("stock.admin", "deps_install", limit=3, window=600)
def deps_install():
    """用户确认后安装可选依赖（akshare）。白名单限定 + 幂等 + 子进程执行。"""
    body = request.get_json(silent=True) or {}
    dep = (body.get("dep") or "").strip()
    pkg = _INSTALLABLE.get(dep)
    if pkg is None:
        return jsonify({"success": False, "error": f"unsupported dependency: {dep}"}), 400
    if _probe_optional_dep(dep)["installed"]:
        return jsonify({"success": True, "data": {"dep": dep, "already": True}})
    try:
        # 固定命令、无 shell 拼接，超时 300s（akshare 依赖较多，首次安装较慢）
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-input", pkg],
            capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        _LOGGER.error("install %s timed out after 300s", dep)
        return jsonify({"success": False, "error": "install timed out"}), 500
    except Exception as error:
        _LOGGER.error("install %s failed: %s", dep, error)
        return jsonify({"success": False, "error": "install failed"}), 500
    if proc.returncode != 0:
        _LOGGER.error("install %s pip error: %s", dep, (proc.stderr or "").strip()[-500:])
        return jsonify({"success": False, "error": "install failed"}), 500
    installed = _probe_optional_dep(dep)
    return jsonify({"success": installed["installed"],
                    "data": {"dep": dep, "installed": installed["installed"]}})


# ── D1-a：桌面端行情端点（契约 §3）──
# 注意：桌面端专用端点返回契约 envelope {ok, data, error, meta}，
# 与旧端点 {success, ...} 风格并存；401/403/429 仍由 _admin_required 统一处理。

def _contract_error(message: str, status: int):
    return jsonify({"ok": False, "data": None, "error": message, "meta": None}), status


# 业务错误 → HTTP 状态码映射（修复 S2-5：旧实现无条件 200 + data.error）
_ERROR_STATUS = {
    "universe too small": 422,      # 语义可解但样本不足
    "unknown factor": 400,
    "requires": 503,                # 数据源不可用
    "DATA_UNAVAILABLE": 503,
}


def _contract_result(result, default_ok_status: int = 200):
    """统一收口分析类端点的"业务错误 vs 成功"契约。

    背景：unwrap() 在 ok===false 或 data===null 时抛错，故改判决不会破坏前端；
    而旧行为（ok:true + data.error）会让前端把错误对象当结果渲染。
    """
    if isinstance(result, dict) and result.get("error"):
        msg = str(result["error"])
        code = (_ERROR_STATUS.get(result.get("error_code") or "")
                or next((v for k, v in _ERROR_STATUS.items() if k in msg), 422))
        return jsonify({"ok": False, "data": None, "error": msg,
                        "error_code": result.get("error_code"), "meta": None}), code
    return jsonify({"ok": True, "data": result, "error": None,
                    "meta": _generated_meta()}), default_ok_status


def _kb_unavailable(op: str):
    """研报知识库后端不可用（project_workspace 未装载/被发行版门控排除）→ 503。

    与 500 的区别必须保留：这是「依赖未就绪」的可预期降级（发行版差异即可触发），
    不是本插件的代码缺陷；503 + kb_unavailable 让桌面端可给出「本版本不含研报库」
    的确定文案，而不是把裸栈当业务错误展示。
    """
    _LOGGER.warning("kb op %s degraded: knowledge base backend unavailable", op)
    return (jsonify({"ok": False, "data": None,
                      "error": "knowledge base unavailable: project_workspace plugin "
                               "is not loaded in this edition",
                      "meta": {"code": "kb_unavailable", "op": op, "retryable": True}}),
            503)


@stock_analysis_bp.get("/api/kline")
@_perm_required("stock.read", "kline", limit=60, window=60.0)
def kline():
    """桌面端 K 线 + 服务端权威指标（契约 §3.1）。period 现仅 daily，其余 501。"""
    symbol, sym_err = _symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    period = request.args.get("period", "daily").lower()
    adjust = request.args.get("adjust", "hfq").lower()
    limit = request.args.get("limit", 120, type=int)
    offset = request.args.get("offset", 0, type=int)
    until = request.args.get("until", "").strip() or None
    try:
        from .kline_service import KlineUnavailableError, UnsupportedPeriodError, kline_payload
        payload = kline_payload(symbol, period=period, adjust=adjust,
                                limit=limit, offset=offset, until=until)
    except UnsupportedPeriodError as error:
        return _contract_error(str(error), 501)
    except Exception as error:
        _LOGGER.error("kline failed symbol=%s period=%s: %s", symbol, period, error)
        return _contract_error("kline unavailable", 502)
    return jsonify(payload)


@stock_analysis_bp.get("/api/quotes")
@_perm_required("stock.read", "quotes", limit=120, window=60.0)
def quotes():
    """桌面端批量行情快照（契约 §3）。symbols ≤ 50 逗号分隔，fields 可选。"""
    raw = request.args.get("symbols", "").strip()
    if not raw:
        return _contract_error("symbols is required", 400)
    symbols = [s.strip() for s in raw.split(",") if s.strip()]
    if not symbols:
        return _contract_error("symbols is required", 400)
    if len(symbols) > 50:
        return _contract_error("symbols too many (max 50)", 400)
    for s in symbols:
        if len(s) > 12 or not s.isascii() or not s.replace(".", "").isalnum():
            return _contract_error(f"invalid symbol: {s}", 400)
    fields = request.args.get("fields", "").strip() or None
    try:
        from .kline_service import QuoteUnavailableError, quotes_payload
        payload = quotes_payload(symbols, fields=fields)
    except Exception as error:
        _LOGGER.error("quotes failed symbols=%s: %s", raw, error)
        return _contract_error("quotes unavailable", 502)
    return jsonify(payload)


# ── D1-b / D1-c：桌面端异步任务 + 告警（契约 §3）──

def _generated_meta():
    from datetime import datetime
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


@stock_analysis_bp.post("/api/jobs")
@_perm_required("stock.write", "jobs_create", limit=10, window=60.0)
def jobs_create():
    """创建异步分析任务。scope=full|technical；同日已成功且未 force → reuse 复用。"""
    _sa()                                  # 建表兜底
    body = request.get_json(silent=True) or {}
    symbol, sym_err = _body_symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    scope = (body.get("scope") or "full").lower()
    if scope not in {"full", "technical"}:
        return _contract_error("scope must be 'full' or 'technical'", 400)
    if (body.get("type") or "analyze").lower() != "analyze":
        return _contract_error("unsupported job type (only 'analyze')", 400)
    force = bool(body.get("force"))
    try:
        from .jobs_queue import submit_job
        payload = submit_job(symbol, scope=scope, force=force)
    except Exception as error:
        _LOGGER.error("job submit failed symbol=%s scope=%s: %s", symbol, scope, error)
        return _contract_error("job submit failed", 502)
    return jsonify({"ok": True, "data": payload, "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/jobs")
@_perm_required("stock.read", "jobs_list", limit=60, window=60.0)
def jobs_list():
    """工作流任务列表（契约 §3 列表视图）。

    ?status=queued|running|done|failed 可选过滤；?limit=1..200（默认 50）。
    补齐列表视图：此前仅有 POST /api/jobs 与 GET /api/jobs/<job_id>，
    前端 fetchJobs() 命中 404 后静默降级，导致 WorkflowListV3 恒为空。
    """
    _sa()                                   # 建表兜底：冷启动首调不再 500
    try:
        limit = request.args.get("limit", 50, type=int)
        status = request.args.get("status") or None
        if status and status not in {"queued", "running", "done", "failed"}:
            return _contract_error("status must be one of queued|running|done|failed", 400)
        from .models_sa import list_jobs
        rows = list_jobs(limit=limit, status=status)
    except Exception as error:
        _LOGGER.error("jobs list failed: %s", error)
        return _contract_error("jobs list unavailable", 500)
    # 契约对齐前端 fetchJobs()：data 为 {"jobs":[...]}（见 stockApi.ts 的 TODO 期望）
    return jsonify({"ok": True, "data": {"jobs": rows}, "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/flow/spans")
@_perm_required("stock.read", "flow_spans", limit=60, window=60.0)
def flow_spans():
    """神经中枢 flow span 回放（P1，方案 §6.1.6）——只读。

    - 数据源：sa_flow_spans 归档表（保留 30 天），非出流表（sa_sse_events 仅 1 天实时通道）；
    - 查询：?from=/to=（ISO 时间）时间窗、?trace_id= 按贯穿 id 聚合、?limit=1..1000；
    - 排序：按 id/created_at 升序（双时基纪律 §5.1，payload.ts 仅前端显示）。
    管理端管理员/运营视角（stock.read 权限口径，§7.10.4）。
    """
    sa = _sa()
    try:
        from_ts = request.args.get("from")
        to_ts = request.args.get("to")
        trace_id = request.args.get("trace_id") or None
        limit = request.args.get("limit", 200, type=int)
        rows = sa.list_flow_spans(from_ts=from_ts, to_ts=to_ts,
                                  trace_id=trace_id, limit=limit)
    except Exception as error:
        _LOGGER.error("flow spans list failed: %s", error)
        return _contract_error("flow spans unavailable", 500)
    return jsonify({"ok": True, "data": {"spans": rows}, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/agents/runtime")
@_perm_required("stock.read", "agents_runtime", limit=60, window=60.0)
def agents_runtime():
    """智能体实时负载 + 认知进化统计（方案 §3.2）。

    队列数据来自本插件 sa_jobs；认知进化等级来自 **memory_engine** 插件
    （PG 的 memory_engine schema：evolution_rounds / reflexion_logs）。
    memory_engine 不可用时**优雅降级**（rounds/xp 记 0），不影响队列部分——
    与「未配置 provider → 优雅空态」同一口径，不整页报错。
    """
    _sa()
    try:
        from .models_sa import count_jobs
        queued = count_jobs(status="queued")
        running = count_jobs(status="running")
    except Exception as error:
        _LOGGER.error("jobs count failed: %s", error)
        return _contract_error("agents runtime unavailable", 500)

    rounds = 0
    lessons = 0
    cog_ok = False
    try:
        from plugins.memory_engine.models import get_memory_engine_db
        mconn = get_memory_engine_db()
        try:
            r1 = mconn.execute(
                "SELECT COUNT(*) n FROM evolution_rounds WHERE status = 'closed'").fetchone()
            r2 = mconn.execute("SELECT COUNT(*) n FROM reflexion_logs").fetchone()
            rounds = int(r1["n"]) if r1 else 0
            lessons = int(r2["n"]) if r2 else 0
            cog_ok = True
        finally:
            mconn.close()
    except Exception as me_err:
        # 跨插件读取失败属可降级别（memory_engine 未装载/表未建），不影响主数据
        _LOGGER.warning("memory_engine stats unavailable (degrade): %s", me_err)

    return jsonify({
        "ok": True,
        "data": {
            "queued": queued,
            "running": running,
            "lv": 1 + rounds,      # 近似：每完成一轮进化升 1 级
            "xp": lessons,         # 近似：反思条数作经验值
            "cogev": cog_ok,       # 认知进化数据是否取到（前端据此决定是否标 mock）
            "skills": [],
        },
        "error": None,
        "meta": _generated_meta(),
    })


@stock_analysis_bp.get("/api/sectors")
@_perm_required("stock.read", "sectors", limit=60, window=60.0)
def sectors():
    """板块热力：申万一级行业（31 个）× 成分股实时涨跌幅。

    ★ 方法论必须如实告知前端（meta.note）—— 申万**指数**行情没有免费源
      （akshare 该版本无 sw_index_daily/quote；index_zh_a_hist('801010') 走东财
      push2，本机代理不通；新浪 562 个指数快照里不含申万 801xxx），
      所以这里的涨跌幅是**成分股聚合出来的**，不等于申万指数官方涨跌：
      - method=full_snapshot（默认首选）：新浪全市场快照（实测 5564 行 / 36.5s，
        **当日落盘缓存**，命中后毫秒级）→ 按**权重加权**平均，覆盖全部成分股；
      - method=sampled_quotes（快照源失败时回退）：腾讯逐只取样本（默认每行业
        权重前 8 只）→ 等权均值，覆盖率为样本数/成分数（**如实返回，不假装全量**）。

    行业分类来自 sa_classification / sa_sector_constituent，由 `POST /api/sectors/ingest`
    从 akshare 申万免费源灌入（无需 Tushare 积分）。两者皆空 → 404 + 明确 error，
    前端据此显示"需先灌入行业分类"，不用 mock 顶替。
    """
    per_sector = request.args.get("per_sector", 8, type=int)
    max_symbols = request.args.get("max_symbols", 600, type=int)
    method = (request.args.get("method") or "auto").strip().lower()

    # 成分股：权重明细表优先（有权重才能做加权），否则退回分类表（等权采样）
    grouped: dict = {}
    counts: dict = {}
    try:
        from .models_sa import sector_constituents
        consts = sector_constituents(standard="sw") or {}
        for ind, items in consts.items():
            grouped[ind] = {"standard": "sw", "sector": ind, "members": items}
    except Exception as error:
        _LOGGER.warning("sectors constituents unavailable (fallback): %s", error)
        consts = {}
    if not grouped:
        try:
            from .models_sa import sector_member_counts, sector_symbols
            legacy = sector_symbols(per_sector=per_sector, max_symbols=max_symbols) or {}
            # ★ 分母必须取行业**真实成分总数**：若取采样数会出现 "8/8 = 100% 覆盖"
            #   的假象（实际是 5218 只里取了 8 只），覆盖率就失去了提示意义。
            counts = sector_member_counts(standard="sw") or {}
            for g in legacy.values():
                grouped[g["sector"]] = {
                    "standard": g["standard"], "sector": g["sector"],
                    "members": [{"symbol": f"CN:{s}", "code": s, "name": None,
                                 "weight": None} for s in g["symbols"]],
                    "total": counts.get(g["sector"], len(g["symbols"])),
                }
        except Exception as error:
            _LOGGER.error("sectors classification failed: %s", error)
            return _contract_error("sectors unavailable", 500)
    if not grouped:
        return _contract_error(
            "sectors unavailable (no classification data; POST /api/sectors/ingest first)",
            404,
        )

    # 2) 行情：全市场快照（当日缓存）优先；失败回退逐只采样
    snap = None
    quotes = {}
    used = "sampled_quotes"
    if method in ("auto", "snapshot", "full"):
        try:
            from .sector_sw import market_snapshot
            snap = market_snapshot()
            used = "full_snapshot"
        except Exception as error:
            _LOGGER.warning("sectors full snapshot unavailable: %s", error)
            snap = None
    if snap is None:
        symbols = sorted({m["code"] for g in grouped.values()
                          for m in g["members"][:per_sector] if m.get("code")})
        symbols = symbols[:max_symbols]
        try:
            from .kline_service import quotes_payload
            for i in range(0, len(symbols), 50):        # tencent 批量上限 50
                payload = quotes_payload(symbols[i:i + 50]) or {}
                for q in (payload.get("data") or {}).get("quotes") or []:
                    if q.get("symbol"):
                        quotes[str(q["symbol"]).split(":")[-1]] = q
        except Exception as error:
            _LOGGER.warning("sectors quotes failed: %s", error)
            return _contract_error("sectors quotes unavailable", 502)

    # 3) 聚合
    rows = []
    matched = 0
    for ind, g in grouped.items():
        members = g["members"]
        pool = members if snap is not None else members[:per_sector]
        wsum = 0.0
        wacc = 0.0
        vals = []
        up = down = 0
        leader = None
        for m in pool:
            code = m.get("code") or ""
            if snap is not None:
                q = snap.get(code)
                pct = q.get("change_pct") if q else None
                name = (q or {}).get("name") or m.get("name")
            else:
                q = quotes.get(code)
                pct = q.get("change_pct") if q else None
                name = m.get("name")
            if pct is None:
                continue
            pct = float(pct)
            matched += 1
            vals.append(pct)
            w = m.get("weight")
            if w:
                wsum += float(w)
                wacc += float(w) * pct
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            if leader is None or pct > leader[0]:
                leader = (pct, code, name)
        if not vals:
            continue
        avg = round(sum(vals) / len(vals), 4)
        rows.append({
            "sector": ind,
            "standard": g["standard"],
            "avgChangePct": avg,
            "weightedChangePct": round(wacc / wsum, 4) if wsum else None,
            "count": len(vals),
            "members": g.get("total") or len(members),
            "up": up,
            "down": down,
            "leader": ({"symbol": leader[1], "name": leader[2], "changePct": leader[0]}
                       if leader else None),
        })
    rows.sort(key=lambda r: (r["weightedChangePct"]
                             if r["weightedChangePct"] is not None else r["avgChangePct"]),
              reverse=True)
    if not rows:
        return _contract_error("sectors unavailable (no live quotes for classified symbols)", 404)

    total_members = sum(r["members"] for r in rows)
    note = ("行业涨跌幅由成分股聚合得出（申万指数无免费行情源）："
            "full_snapshot=全成分按权重加权，sampled_quotes=样本等权均值")
    return jsonify({
        "ok": True,
        "data": {"sectors": rows, "quoteCount": matched},
        "error": None,
        "meta": {**_generated_meta(), "method": used,
                 "coverage": {"industries": len(rows), "matched": matched,
                              "members": total_members},
                 "note": note},
    })


@stock_analysis_bp.post("/api/sectors/ingest")
@_perm_required("stock.write", "sectors_ingest", limit=6, window=60.0)
def sectors_ingest():
    """灌入申万一级行业分类（akshare 免费源，**不需要 Tushare 积分**）。

    实测：31 个行业 / 5218 条成分，约 10.5s（命中 7 天成分缓存后更快）。
    幂等：分类按 (symbol, standard, effective_from) UPSERT；成分明细按
    (standard, industry_code, symbol, as_of) 先删后插。

    query: industries=801010,801030（可选，默认全部 31 个）；refresh=1 忽略本地缓存。
    """
    only = request.args.get("industries", "").strip()
    use_cache = request.args.get("refresh", "0") not in ("1", "true", "True")
    try:
        from .sector_sw import ingest
        stats = ingest([c for c in only.split(",") if c.strip()] or None,
                       use_cache=use_cache)
    except Exception as error:
        _LOGGER.error("sectors ingest failed: %s", error)
        return _contract_error(f"sectors ingest failed: {type(error).__name__}: {error}", 502)
    if not stats.get("members"):
        return _contract_error("sectors ingest produced no rows (akshare unavailable?)", 502)
    return jsonify({"ok": True, "data": stats, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/sectors/valuation")
@_perm_required("stock.read", "sectors_valuation", limit=60, window=60.0)
def sectors_valuation():
    """申万一级行业估值（官方口径，非聚合）：PE-TTM / PB / 静态股息率 / 成份个数。

    源为 `akshare.sw_index_first_info`（申万官网口径，免费，实测 2.2s / 31 行），
    当日落盘缓存。这是**指数官方估值**，与 /api/sectors 里由成分股聚合出的涨跌幅
    性质不同，前端不要混用口径。
    """
    try:
        from .sector_sw import industry_info
        rows = industry_info()
    except Exception as error:
        _LOGGER.warning("sectors valuation failed: %s", error)
        return _contract_error(f"sectors valuation unavailable: {type(error).__name__}: {error}",
                               502)
    return jsonify({"ok": True, "data": {"industries": rows, "count": len(rows)},
                    "error": None,
                    "meta": {**_generated_meta(), "source": "akshare_sw",
                             "note": "申万官网口径行业估值（PE-TTM/PB/静态股息率）"}})


@stock_analysis_bp.get("/api/gateway/health")
@_perm_required("stock.read", "gateway_health", limit=60, window=60.0)
def gateway_health():
    """数据源运行期健康指标（方案 §4.4）。

    与 `/api/providers` 的区别：后者是**静态元信息**（是否配凭据 / 覆盖类别），
    这里是**运行期实测**（调用数、命中率、平均与 P95 延迟、冷却剩余、最后成功时间）。

    口径必须如实告知前端（meta.note）：
      - 进程内计数，**重启清零**，不落库；
      - 只在有 provider 被调用后才有数据，冷启动返回空数组（前端显示"暂无调用"）；
      - 冷却为估算值（令牌桶耗尽时按 30s 计）。
    """
    try:
        from .metrics_collector import snapshot as _snap, meta as _meta
        rows = _snap()
        note = _meta()
    except Exception as error:
        _LOGGER.error("gateway health unavailable: %s", error)
        return _contract_error("gateway health unavailable", 500)
    return jsonify({
        "ok": True,
        "data": {"providers": rows, "note": note},
        "error": None,
        "meta": _generated_meta(),
    })


@stock_analysis_bp.get("/api/system/throughput")
@_perm_required("stock.read", "sys_throughput", limit=60, window=60.0)
def sys_throughput():
    """落盘吞吐：近 60s PG 写入行数/s（方案 §4.5）。

    埋点在 `models_sa.get_db()` 的连接代理上（INSERT/UPDATE/DELETE 按 cursor.rowcount 计），
    插件全部写路径自动覆盖。进程内计数、重启清零、不落库（与 §4.4 同口径）。

    权限：方案示例写的是 `stock.admin`，这里下调为 `stock.read` —— 该端点是只读展示
    指标，与 §4.4 `/api/gateway/health` 同级，按 admin 收会在只读账号下 403，
    卡片永远拿不到数。若要收紧，改这一行即可。

    从未落盘 → rowsPerSec=null（前端显示 --，不用 0 冒充"未采集"）；
    有落盘但近 60s 无写入 → 0.0（真实为 0）。
    """
    try:
        from .metrics_collector import sink_rate, sink_meta
        note = sink_meta()
        rate = sink_rate(note["windowSec"])
    except Exception as error:
        _LOGGER.error("throughput unavailable: %s", error)
        return _contract_error("throughput unavailable", 500)
    return jsonify({
        "ok": True,
        "data": {
            "rowsPerSec": rate,
            "unit": "rows/s",
            "windowSec": note["windowSec"],
            "rowsTotal": note["rowsTotal"],
            "lastWrite": note["lastWrite"],
            "note": note,
        },
        "error": None,
        "meta": _generated_meta(),
    })


@stock_analysis_bp.get("/api/quote/depth")
@_perm_required("stock.read", "depth", limit=120, window=60.0)
def quote_depth():
    """五档盘口（方案 §4.2）。

    数据源：腾讯快照自带的买卖五档（**免 key**），终端桥 wind/choice 待其支持 DEPTH
    后由 gateway 自动进链。与方案示例的差异：方案假设必须有终端桥（否则 404），
    实测腾讯快照 88 字段里就含五档，故无终端也能出真实数据 —— 只有两端都不可用时才 404。

    量单位为**手**；`buy` 买一…买五（价降序），`sell` 卖一…卖五（价升序）。
    """
    symbol, sym_err = _symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    try:
        from .gateway import gateway
        data = gateway.get_depth(symbol)
    except Exception as error:
        _LOGGER.warning("depth unavailable symbol=%s: %s", symbol, error)
        return _contract_error("depth unavailable (no provider could serve order book)", 404)
    if not isinstance(data, dict) or (not data.get("buy") and not data.get("sell")):
        return _contract_error("depth unavailable (empty order book)", 404)
    return jsonify({"ok": True, "data": {"symbol": symbol, **data},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/quote/ticks")
@_perm_required("stock.read", "ticks", limit=120, window=60.0)
def quote_ticks():
    """分笔成交（方案 §4.3）。

    数据源：腾讯逐笔明细（**免 key**）。未开盘/无成交时上游返回空 → 404 + 明确 error，
    前端显示"暂无逐笔（可能未开盘）"，不用 mock 顶替。
    """
    symbol, sym_err = _symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 500))
    try:
        from .gateway import gateway
        data = gateway.get_ticks(symbol, limit=limit)
    except Exception as error:
        _LOGGER.warning("ticks unavailable symbol=%s: %s", symbol, error)
        return _contract_error("ticks unavailable (no provider could serve ticks)", 404)
    rows = (data or {}).get("rows") if isinstance(data, dict) else None
    if not rows:
        return _contract_error("ticks unavailable (no trades yet; market may be closed)", 404)
    return jsonify({"ok": True, "data": {"symbol": symbol, "rows": rows},
                    "error": None, "meta": _generated_meta()})


_CN_COUNTRY = ("CN", "CHN", "CHINA", "ZH")

# /api/macro/catalog 的国家候选：只列内核有明确事实来源的国家 ——
#   CN：akshare_macro 免费源（providers/akshare_macro.py:40 别名集，指标口径见 :44 _SPEC）
#   US：FMP /economic 的默认国家（providers/fmp_provider.py:288 与 gateway.py:350 的
#       country="US" 签名默认值）
# FMP 的 country 是自由参数、内核无国家枚举（fmp_provider.py:231 原样透传），
# 故不臆造 JP/GB/DE/FR 等清单；候选国家在 catalog 内还要「指标清单非空」才下发。
_MACRO_CANDIDATE_COUNTRIES = ("CN", "US")


def _fmp_configured() -> bool:
    """FMP 是否配了 key。未配置时其指标清单**不并入下拉** —— 否则用户选了必然 404。"""
    try:
        from .providers.fmp_provider import FMPProvider
        return bool(FMPProvider().health().get("configured"))
    except Exception as err:                         # noqa: BLE001
        _LOGGER.warning("FMP health probe failed: %s", err)
        return False


def _macro_indicators(country: str) -> list:
    """可选指标清单随国家变：中国由 akshare 免费源供给（无需 key），其余走 FMP。

    取不到清单时**记日志**再返回已收集的部分 —— 不清空页面下拉，也不静默吞异常
    （ensure_tables 那次「except: pass」吞掉建表失败的教训）。
    """
    out: list = []
    if country in _CN_COUNTRY:
        try:
            from .providers.akshare_macro import AkshareMacroProvider
            out.extend(AkshareMacroProvider.MACRO_INDICATORS)
        except Exception as err:                     # noqa: BLE001
            _LOGGER.warning("akshare_macro indicators unavailable: %s", err)
    try:
        from .providers.fmp_provider import FMPProvider
        # 未配 key 时 FMP 全部指标都取不到数，列进下拉只会让用户点到 404
        if _fmp_configured():
            out.extend(i for i in FMPProvider.MACRO_INDICATORS if i not in out)
    except Exception as err:                         # noqa: BLE001
        _LOGGER.warning("FMP macro indicators unavailable: %s", err)
    return out


@stock_analysis_bp.get("/api/macro")
@_perm_required("stock.read", "macro", limit=60, window=60.0)
def macro():
    """宏观 EDB 指标时序（方案 §4.6）。

    数据源（B 段 S3 起两条路）：
      - **中国（country=CN）**：`akshare_macro` 免费源（国家统计局口径），无需任何 key；
        FMP 未配 key 时自动落到这条（gateway ROUTE[MACRO] = [FMP, akshare_macro]）。
      - **其他国家**：FMP `/economic`（v3，`?name=<indicator>&country=<CC>`），**需要 FMP key**，
        未配置 → 404 + 明确 error，前端据此走"需配置 FMP 凭据"空态，不用 mock 顶替
        （与 §3.1 一致预期、§4.1 板块热力同款口径）。

    返回的 series 为升序 `[{date, value}]`；`source` 取数据自带溯源（df.attrs），
    跨缓存回读丢失时退化为 "unknown"（如实标注，不编造来源）。
    """
    indicator = (request.args.get("indicator") or "CPI").strip()[:64]
    # 默认 CN：唯一免费源 akshare_macro 只支持 CN，默认 US 会让不带 country 的调用必然 404。
    country = (request.args.get("country") or "CN").strip().upper()[:8]
    limit = request.args.get("limit", 240, type=int) or 240
    limit = max(1, min(limit, 2000))

    try:
        from .gateway import gateway
        df = gateway.get_macro(indicator, country, limit=limit)
    except Exception as error:
        _LOGGER.warning("macro unavailable indicator=%s country=%s: %s",
                        indicator, country, error)
        return _contract_error("macro unavailable (provider not configured)", 404)

    if df is None or getattr(df, "empty", True):
        return _contract_error(f"macro unavailable (no data for {indicator})", 404)

    series = []
    try:
        for idx, row in df.iterrows():
            v = row.get("value")
            series.append({
                "date": str(idx.date()),
                "value": (None if v is None or v != v else float(v)),   # NaN != NaN
            })
    except Exception as error:                       # noqa: BLE001
        _LOGGER.error("macro series build failed: %s", error)
        return _contract_error("macro unavailable (bad payload)", 500)

    attrs = getattr(df, "attrs", {}) or {}
    vals = [s["value"] for s in series if s["value"] is not None]
    latest = vals[-1] if vals else None
    prev = vals[-2] if len(vals) > 1 else None
    indicators = _macro_indicators(country)

    return jsonify({
        "ok": True,
        "data": {
            "indicator": attrs.get("indicator") or indicator,
            "country": attrs.get("country") or country,
            "source": attrs.get("source") or "unknown",
            "asOf": series[-1]["date"] if series else None,
            "series": series,
            "latest": latest,
            "prev": prev,
            "changePct": (round((latest - prev) / abs(prev) * 100, 4)
                          if (latest is not None and prev not in (None, 0)) else None),
            "indicators": indicators,
            # 口径随数据走（akshare_macro 在 df.attrs 里给出）：单位是 % / 亿元 / 指数，
            # 频率是 月 / 季 / 日。跨缓存回读丢失时为 None（如实留空，不猜）。
            "unit": attrs.get("unit"),
            "freq": attrs.get("freq"),
        },
        "error": None,
        "meta": _generated_meta(),
    })


@stock_analysis_bp.get("/api/macro/catalog")
@_perm_required("stock.read", "macro_catalog")
def macro_catalog():
    """宏观 EDB 指标 catalog（只读下发，壳层据此渲染国家/指标下拉，不再各自镜像）。

    事实来源：
      - 指标 code / 单位 / 频率（仅 CN）：`providers/akshare_macro.py:44 _SPEC`
        （indicator → akshare 函数, 日期列, 值列, 单位, 频率）。
      - 指标可用性：复用 `/api/macro` 的 `_macro_indicators`（同文件 `:1288`），
        它已含「FMP 未配 key 就不并入清单」的门控 —— 复用而非另写一份，
        保证 catalog 与 `/api/macro` 返回的 `indicators` 口径一致，不漂移。
      - 国家：见 `_MACRO_CANDIDATE_COUNTRIES` 注释；指标清单为空的国家不下发。
    FMP 侧指标无单位/频率事实来源（`FMPProvider.MACRO_INDICATORS` 只有 code），
    故这两个字段省略而不是填占位值。
    """
    from .providers import akshare_macro as ak_macro

    spec = getattr(ak_macro, "_SPEC", {}) or {}
    countries = []
    for code in _MACRO_CANDIDATE_COUNTRIES:
        codes = _macro_indicators(code)
        if not codes:
            continue
        indicators = []
        for ind in codes:
            row = {"code": ind}
            # CN 指标的口径（单位/频率）随 _SPEC 走；不在表内的（FMP 侧）如实留空
            if code in _CN_COUNTRY and ind in spec:
                row["unit"] = spec[ind][3]
                row["freq"] = spec[ind][4]
            indicators.append(row)
        countries.append({"code": code, "indicators": indicators})
    return jsonify({"ok": True, "data": {"countries": countries},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/consensus")
@_perm_required("stock.read", "consensus", limit=60, window=60.0)
def consensus():
    """分析师一致预期 + 预期差（方案 §3.1：加薄路由，数据插件内已有）。

    - 一致预期：`gateway.get_consensus`（FMP 美股 analyst-estimates / Tushare A 股业绩预告）
    - 预期差：`expectation_gap.compute_gap(consensus, actuals, symbol)`
    - 实际业绩 actuals：`gateway.get_fundamental`（四表合一）；取不到时退化为 {}，
      compute_gap 内部会给出 narrative="无实际业绩数据"，不视为错误。
    provider 未配置 → 404，前端据此走优雅空态（与"未配置 provider"既有口径一致）。
    """
    symbol, sym_err = _symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    try:
        from .gateway import gateway
        from .expectation_gap import compute_gap
        raw = gateway.get_consensus(symbol)
    except Exception as error:
        _LOGGER.warning("consensus unavailable symbol=%s: %s", symbol, error)
        return _contract_error("consensus unavailable (provider not configured)", 404)

    actuals = {}
    try:
        from .gateway import gateway as _gw
        _fund = _gw.get_fundamental(symbol)
        actuals = _fund if isinstance(_fund, dict) else {}
    except Exception as f_err:
        # 财报取不到只影响"预期差"，一致预期本身仍可返回
        _LOGGER.info("fundamental unavailable for gap symbol=%s: %s", symbol, f_err)

    gap = compute_gap(raw, actuals, symbol)
    items = [{
        "metric": it.metric,
        "label": it.label,
        "actual": it.actual,
        "estimate": it.estimate,
        "estimate_low": it.estimate_low,
        "estimate_high": it.estimate_high,
        "surprise_pct": it.surprise_pct,
        "unit": it.unit,
        "beat": it.beat,
    } for it in (gap.items or [])]

    return jsonify({
        "ok": True,
        "data": {
            "symbol": symbol,
            "consensus": raw if isinstance(raw, (dict, list)) else None,
            "gap": {
                "symbol": gap.symbol,
                "period": gap.period,
                "source": gap.source,
                "overall_surprise": gap.overall_surprise,
                "verdict": gap.verdict,
                "narrative": gap.narrative,
                "items": items,
            },
        },
        "error": None,
        "meta": _generated_meta(),
    })


@stock_analysis_bp.get("/api/jobs/<job_id>")
@_perm_required("stock.read", "jobs_status", limit=60, window=60.0)
def jobs_status(job_id):
    """查询任务状态。queued|running|done|failed；done 附 result，failed 附 error{code,message}。"""
    if not _valid_job_id(job_id):
        return _contract_error("invalid job_id", 400)
    _sa()                                   # 建表兜底：冷启动首调不再 500
    try:
        from .jobs_queue import get_job_status
        payload = get_job_status(job_id)
    except Exception as error:
        _LOGGER.error("job status failed job_id=%s: %s", job_id, error)
        return _contract_error("job status unavailable", 500)
    if payload is None:
        return _contract_error("job not found", 404)
    return jsonify({"ok": True, "data": payload, "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/discuss")
@_perm_required("stock.write", "discuss", limit=10, window=60.0)
def discuss_create():
    """多空对辩研判（接线点①，阶段 B 模式 A：异步任务）。

    body {"symbol"} → 入队（scope/type=discuss）并返回 {job_id, status}；
    状态与结果走既有 GET /api/jobs/<job_id> 契约（done 附 result，含
    report/signal/rounds）。4 轮 LLM 在队列线程执行，规避同步长链路超时。
    同一标的同日已 done 且未 force → reuse 复用（幂等，KB 不重复插入）。
    """
    _sa()                                  # 建表兜底（冷启动首调不再 500）
    symbol, sym_err = _body_symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    try:
        from .jobs_queue import submit_discuss_job
        payload = submit_discuss_job(symbol)
    except Exception as error:
        _LOGGER.error("discuss submit failed symbol=%s: %s", symbol, error)
        return _contract_error("discuss submit failed", 502)
    return jsonify({"ok": True, "data": payload, "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/research/report")
@_perm_required("stock.write", "research_report", limit=5, window=60.0)
def research_report_create():
    """AI 深度研报（方案 §2③）—— 异步任务，避免同步长链路超时。

    body {"symbol", "force"?: bool} → 入队（type/scope=research）并返 {job_id, status, reuse}。
    轮询走 GET /api/research/report/<job_id>（同一 sa_jobs 表，但**只认 research 任务**，
    避免把 analyze/discuss 任务的结果张冠李戴）。

    ★ 无 LLM key 时不会假装成功：deep_research 节点返回 success=False，
      任务落 failed 并在 error 里保留原文（如 "Agent call failed"/"no provider"），
      UI 据此区分"没配 key"与"跑挂了"。
    """
    _sa()                                  # 建表兜底（冷启动首调不再 500）
    symbol, sym_err = _body_symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    force = request.get_json(silent=True) or {}
    force = bool(force.get("force"))
    try:
        from .jobs_queue import submit_research_job
        payload = submit_research_job(symbol, force=force)
    except Exception as error:
        _LOGGER.error("research report submit failed symbol=%s: %s", symbol, error)
        return _contract_error("research submit failed", 502)
    return jsonify({"ok": True, "data": payload, "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/research/report/<job_id>")
@_perm_required("stock.read", "research_report_status", limit=60, window=60.0)
def research_report_status(job_id):
    """研报任务状态。queued|running|done|failed；done 附 result{report,signal,evidence_chars}。

    与 GET /api/jobs/<id> 的差别：只返回 research 任务 —— 传别的 job_id 返 404，
    而不是把对辩/技术面分析的结果当成研报展示。
    """
    if not _valid_job_id(job_id):
        return _contract_error("invalid job_id", 400)
    _sa()
    try:
        from . import models_sa as sa_module
        row = sa_module.get_job(job_id)
        if row is None or row.get("type") != "research":
            # 任务不存在，或存在但不是研报任务（analyze/discuss）→ 一律 404，
            # 绝不把别的任务结果当研报返回。
            return _contract_error("job not found", 404)
        from .jobs_queue import get_job_status
        payload = get_job_status(job_id)
    except Exception as error:
        _LOGGER.error("research status failed job_id=%s: %s", job_id, error)
        return _contract_error("research status unavailable", 500)
    if payload is None:
        return _contract_error("job not found", 404)
    return jsonify({"ok": True,
                    "data": {**payload, "kind": "research"},
                    "error": None, "meta": _generated_meta()})


def _valid_job_id(job_id: str) -> bool:
    """j- 前缀 + 6~16 位十六进制（防注入，避免引入 re 依赖）。"""
    return (job_id.startswith("j-") and 6 <= len(job_id) - 2 <= 16
            and all(ch in "0123456789abcdef" for ch in job_id[2:]))


# ── D1-c：桌面端告警规则（契约 §3 /api/alerts）──

def _valid_time(s: str) -> bool:
    """HH:MM 静默窗口校验（避免引入 re 依赖）。"""
    if not s or len(s) != 5 or s[2] != ":":
        return False
    hh, mm = s[:2], s[3:]
    return (hh.isdigit() and mm.isdigit()
            and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59)


@stock_analysis_bp.get("/api/alerts")
@_perm_required("stock.read", "alerts_list")
def alerts_list():
    """告警规则列表（可选 symbol 过滤）。status 枚举与桌面零转换。"""
    symbol = request.args.get("symbol", "").strip() or None
    try:
        sa = _sa()
        from .alert_engine import serialize_rule
        alerts = [serialize_rule(r) for r in sa.list_alerts(symbol=symbol)]
    except Exception as error:
        _LOGGER.error("alerts list failed symbol=%s: %s", symbol, error)
        return _contract_error("alerts unavailable", 500)
    return jsonify({"ok": True, "data": {"alerts": alerts},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/alerts")
@_perm_required("stock.write", "alerts_create", limit=20, window=60.0)
def alerts_create():
    """创建告警规则。type 6 类对齐桌面 ALTER_TYPE_META；signal_change 无需 threshold。"""
    _sa()                                  # 建表兜底
    from .alert_engine import coerce_channel
    body = request.get_json(silent=True) or {}
    symbol, sym_err = _body_symbol()
    if symbol is None:
        return _contract_error(sym_err, 400)
    alert_type = (body.get("type") or "").lower()
    if alert_type not in {"price_above", "price_below", "change_pct",
                          "rsi_oversold", "rsi_overbought", "signal_change"}:
        return _contract_error("unsupported alert type", 400)
    threshold = body.get("threshold")
    if alert_type != "signal_change":
        if threshold is None or threshold == "":
            return _contract_error("threshold is required", 400)
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            return _contract_error("threshold must be a number", 400)
    else:
        threshold = None
    channel_raw = body.get("channel")
    if channel_raw is not None and not isinstance(channel_raw, str):
        return _contract_error("channel must be one of in_app/email/im", 400)
    # 写校验用 coerce_channel（未知值 → None → 400），不用 normalize_channel：
    # 后者"未知回落 in_app"是读侧容错，用在写入会把用户的 wechat/emial 静默改成站内信，
    # 并让下面这类 CHANNEL_CODES 判定永远不成立（契约要求的 400 变成 201）。
    channel = coerce_channel(channel_raw.strip() if isinstance(channel_raw, str) else channel_raw)
    if channel is None:
        return _contract_error("channel must be one of in_app/email/im", 400)
    silent_from = (body.get("silent_from") or "").strip() or None
    silent_to = (body.get("silent_to") or "").strip() or None
    if silent_from is not None and not _valid_time(silent_from):
        return _contract_error("silent_from must be HH:MM", 400)
    if silent_to is not None and not _valid_time(silent_to):
        return _contract_error("silent_to must be HH:MM", 400)
    if (silent_from is None) != (silent_to is None):
        return _contract_error("silent_from and silent_to must be set together", 400)
    try:
        sa = _sa()
        if sa.duplicate_active_alert(symbol, alert_type, threshold):
            return _contract_error("active rule already exists for this symbol/type/threshold", 409)
        name = _lookup_alert_name(symbol)
        alert_id = sa.create_alert(symbol, name, alert_type, threshold,
                                   channel, silent_from, silent_to)
        from .alert_engine import serialize_rule
        dto = serialize_rule(sa.get_alert_row(alert_id))
    except Exception as error:
        _LOGGER.error("alert create failed symbol=%s type=%s: %s", symbol, alert_type, error)
        return _contract_error("alert create failed", 500)
    return jsonify({"ok": True, "data": {"alert": dto},
                    "error": None, "meta": _generated_meta()})


def _lookup_alert_name(symbol: str):
    try:
        from .alert_engine import lookup_name
        return lookup_name(symbol)
    except Exception:
        return None


@stock_analysis_bp.delete("/api/alerts")
@_perm_required("stock.write", "alerts_delete", limit=20, window=60.0)
def alerts_delete():
    """删除告警规则（按 id）。"""
    _sa()
    raw = request.args.get("id", "").strip()
    if not raw.isdigit():
        return _contract_error("id is required", 400)
    try:
        sa = _sa()
        deleted = sa.delete_alert(int(raw))
    except Exception as error:
        _LOGGER.error("alert delete failed id=%s: %s", raw, error)
        return _contract_error("alert delete failed", 500)
    if not deleted:
        return _contract_error("alert not found", 404)
    return jsonify({"ok": True, "data": {"deleted": True, "id": raw},
                    "error": None, "meta": _generated_meta()})


# 告警 status 枚举：模型层已声明（models_sa.py:741「status 枚举与桌面零转换：
# active | triggered | expired | disabled」），读写分支见 models_sa.py:798（disabled 停用）、
# :813（duplicate_active_alert 排除 expired/disabled）、:837（触发 → triggered）、
# :847（解除复位 → active）。此处只做下发，不再让壳层各存一份镜像。
_ALERT_STATUSES = ("active", "triggered", "expired", "disabled")


@stock_analysis_bp.get("/api/alerts/schema")
@_perm_required("stock.read", "alerts_schema")
def alerts_schema():
    """告警 schema（只读下发，壳层据此渲染下拉，不再各自镜像）。

    事实来源全部为插件自身常量，端点内不二次硬编码：
      - type：`alert_engine.ALERT_TYPES`（alert_engine.py:28）
      - type 的 unit / needs_threshold：`alert_engine.ALERT_META`（alert_engine.py:36）
        与 `_NEED_THRESHOLD`（alert_engine.py:44）
      - channel：`alert_engine.CHANNEL_CODES`（alert_engine.py:32）
      - status：`_ALERT_STATUSES`（见上方 models_sa 出处注释）
      - kind：`_ANALYSIS_KINDS`（自选/分析类型，models_sa.py:45 列默认值 'technical'）
    不含 label：文案归壳层 i18n（`stock.type.*` / `v3.al.type.*`），后端不下中文文案。
    """
    from .alert_engine import (ALERT_META, ALERT_TYPES, CHANNEL_CODES,
                               _NEED_THRESHOLD)

    types = [{"value": t,
              "unit": ALERT_META[t][1],
              "needs_threshold": t in _NEED_THRESHOLD}
             for t in sorted(ALERT_TYPES)]
    return jsonify({
        "ok": True,
        "data": {
            "types": types,
            "channels": list(CHANNEL_CODES),
            "statuses": list(_ALERT_STATUSES),
            "kinds": list(_ANALYSIS_KINDS),
        },
        "error": None,
        "meta": _generated_meta(),
    })


@stock_analysis_bp.get("/api/alerts/events")
@_perm_required("stock.read", "alerts_events")
def alerts_events():
    """事件流水回看（可选 alert_id 过滤）。契约信封，新→旧排序。"""
    _sa()
    raw_alert = (request.args.get("alert_id") or "").strip()
    if raw_alert and not raw_alert.isdigit():
        return _contract_error("alert_id must be an integer", 400)
    raw_limit = (request.args.get("limit") or "50").strip()
    if not raw_limit.isdigit():
        return _contract_error("limit must be an integer", 400)
    limit = int(raw_limit)
    if not 1 <= limit <= 200:
        return _contract_error("limit must be between 1 and 200", 400)
    try:
        sa = _sa()
        events = sa.list_alert_events(
            alert_id=int(raw_alert) if raw_alert else None, limit=limit)
    except Exception as error:
        _LOGGER.error("alert events failed alert_id=%s: %s", raw_alert, error)
        return _contract_error("alert events unavailable", 500)
    return jsonify({"ok": True, "data": {"events": events},
                    "error": None, "meta": _generated_meta()})


# SSE 长连接并发闸（DEF-02）：进程内上限默认 2。单 worker（-w 1）下每条 SSE 会占用
# 一条 gthread worker 线程直至断开；无闸时桌面多视图各开一条即可拖垮管理端普通请求。
# FIX-14：上限改为环境变量可配（SA_SSE_MAX_CONNECTIONS），默认值与原实现一致仍为 2。
#   钳制在 [_SSE_LIMIT_MIN, _SSE_LIMIT_MAX]：桌面 native 单进程只有 8 条 waitress 线程，
#   放任配大等于重现「SSE 拖垮管理端」这个原始缺陷；非法值只告警回退，
#   绝不在 import 期抛异常（那会让插件 setup 直接失败）。
_SSE_LIMIT_ENV = "SA_SSE_MAX_CONNECTIONS"
_SSE_LIMIT_DEFAULT = 2
_SSE_LIMIT_MIN = 1
_SSE_LIMIT_MAX = 16


def _resolve_sse_max_connections() -> int:
    raw = (os.environ.get(_SSE_LIMIT_ENV) or "").strip()
    if not raw:
        return _SSE_LIMIT_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        _LOGGER.warning("%s=%r 非整数，回退默认 %d", _SSE_LIMIT_ENV, raw, _SSE_LIMIT_DEFAULT)
        return _SSE_LIMIT_DEFAULT
    clamped = max(_SSE_LIMIT_MIN, min(n, _SSE_LIMIT_MAX))
    if clamped != n:
        _LOGGER.warning("%s=%d 越界，钳制为 %d（允许区间 %d~%d）",
                        _SSE_LIMIT_ENV, n, clamped, _SSE_LIMIT_MIN, _SSE_LIMIT_MAX)
    return clamped


_SSE_MAX_CONNECTIONS = _resolve_sse_max_connections()
_sse_slots = threading.BoundedSemaphore(_SSE_MAX_CONNECTIONS)


@stock_analysis_bp.get("/api/events")
def events_stream():
    """SSE 事件流（契约 §4；D1-d 本轮 topics=alerts,jobs，quotes 暂缓）。

    - 出流表 sa_sse_events 拉取，事件 id = 表自增 id（Last-Event-ID 重连补发）；
    - 首次鉴权（401/403 走 JSON），流内每 30s 重验 JWT，失效发 system.notice 后关闭；
    - 心跳 15s（sse_stream 内实现）；X-Accel-Buffering 关缓冲适配 nginx；
    - DEF-02：长连接并发闸（_SSE_MAX_CONNECTIONS），超限 503 + Retry-After，
      防止多视图各开一条 SSE 耗尽 gthread worker 线程拖垮管理端普通请求。
    """
    from .sse_stream import parse_topics, stream_events
    from services.jwt_service import validate_token

    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.cookies.get("sso_token") or request.headers.get("X-Token")
    payload, auth_err = _require_perm("stock.read")
    if payload is None:
        return jsonify({"success": False, "error": auth_err}), 401
    if auth_err:
        return jsonify({"success": False, "error": auth_err}), 403

    topics, t_err = parse_topics(request.args.get("topics"))
    if topics is None:
        return _contract_error(t_err, 400)
    raw_last = request.headers.get("Last-Event-ID", "").strip()
    last_id = int(raw_last) if raw_last.isdigit() else None

    if not _sse_slots.acquire(blocking=False):
        _LOGGER.warning("sse slots full (%d/%d), rejecting new stream",
                        _SSE_MAX_CONNECTIONS, _SSE_MAX_CONNECTIONS)
        resp = jsonify({"ok": False, "data": None,
                        "error": "too many concurrent event streams",
                        "meta": _generated_meta()})
        resp.status_code = 503
        resp.headers["Retry-After"] = "10"
        return resp

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    def _events():
        try:
            yield from stream_events(topics, last_id, token, validate_token)
        finally:
            _sse_slots.release()

    return Response(
        stream_with_context(_events()),
        mimetype="text/event-stream",
        headers=headers)


# ── 数据源凭据管理（P0-W2）──

_PROVIDER_META = {
    "tushare": {"label": "Tushare", "secret_key": "tushare_token", "market": "CN",
                "categories": ["kline", "fundamental", "moneyflow"]},
    "fmp": {"label": "Financial Modeling Prep", "secret_key": "fmp_api_key", "market": "US",
            "categories": ["kline", "fundamental", "consensus", "profile", "news", "quote", "forecast"]},
    "polygon": {"label": "Polygon.io", "secret_key": "polygon_api_key", "market": "US",
                "categories": ["kline", "quote", "profile", "news"]},
    "akshare": {"label": "AKShare (免费)", "secret_key": None, "market": "CN",
                "categories": ["kline"]},
    "sina": {"label": "新浪财经 (免费)", "secret_key": None, "market": "CN",
             "categories": ["kline", "news"]},
    "tencent": {"label": "腾讯财经 (免费)", "secret_key": None, "market": "CN",
                "categories": ["quote", "index"]},
    "wind": {"label": "Wind 终端桥 (本地)", "secret_key": None, "market": "CN",
             "categories": ["kline", "quote", "index"]},
    "choice": {"label": "Choice 终端桥 (本地)", "secret_key": None, "market": "CN",
               "categories": ["kline", "quote", "index"]},
}


@stock_analysis_bp.get("/api/constants")
@_perm_required("stock.read", "constants")
def constants():
    """插件声明的业务常量（只读下发，壳层据此渲染，不再各自镜像）。

    覆盖：指标版本 / 信号兑现口径 / 陈旧与冷却阈值 / 数据类别 / 路由链路与 TTL /
    各数据源授权标记。数值一律取自模块级权威常量（gateway 的 ROUTE/TTL/COOLDOWN/
    MAX_STALE_DAYS、indicators、signal_quality、reflexion_feedback），端点内不二次硬编码
    —— 这正是前端镜像会漂移的地方（例：前端仍写 14 个 DataCategory，内核已 17 个）。

    鉴权：stock.read（admin 自动放行）；纯读、无副作用，故不做写限流。
    """
    from . import gateway
    from .indicators import INDICATOR_VERSION
    from .reflexion_feedback import DAILY_CAP, MIN_ADVERSE
    from .signal_quality import HORIZONS

    routes = []
    source_authorized: dict = {}
    for category, providers in gateway.ROUTE.items():
        cat = category.value if hasattr(category, "value") else str(category)
        chain = []
        for cls in providers:
            chain.append(cls.name)
            # setdefault：同名 provider 在多条链上出现时沿用首次结果（类属性恒定）
            source_authorized.setdefault(cls.name, bool(getattr(cls, "authorized", False)))
        routes.append({
            "category": cat,
            "chain": chain,
            "ttl": gateway.TTL.get(category, 0),
        })

    data = {
        "indicator_version": INDICATOR_VERSION,
        "realize_params": {
            "horizons": list(HORIZONS),
            "daily_cap": DAILY_CAP,
            "min_adverse": MIN_ADVERSE,
            "basis": "close_hfq",
        },
        "freshness": {
            "max_stale_days": gateway.MAX_STALE_DAYS,
            "cooldown_seconds": gateway.COOLDOWN,
            "cooldown_fail_streak": gateway.COOLDOWN_FAIL_STREAK,
        },
        "data_categories": [c.value for c in gateway.DataCategory],
        "routes": routes,
        "source_authorized": source_authorized,
    }
    return jsonify({"ok": True, "data": data, "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/providers")
@_perm_required("stock.admin", "providers_list")
def providers_list():
    """数据源列表：每个 provider 的健康状态、是否已配置凭据、覆盖类别。"""
    from .crypto import mask as _mask
    pm = current_app.extensions.get("plugin_manager")
    config = pm.get_config("stock_analysis") if pm and pm.is_enabled("stock_analysis") else {}
    result = []
    for name, meta in _PROVIDER_META.items():
        configured = False
        masked = ""
        if meta["secret_key"]:
            val = config.get(meta["secret_key"], "")
            configured = bool(val)
            masked = _mask(val) if val else ""
        result.append({
            "name": name, "label": meta["label"], "market": meta["market"],
            "categories": meta["categories"], "requires_key": bool(meta["secret_key"]),
            "configured": configured, "masked_key": masked,
        })
    return jsonify({"ok": True, "data": {"providers": result},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.put("/api/providers/credentials")
@_perm_required("stock.admin", "providers_save_cred", limit=10, window=60.0)
def providers_save_credential():
    """保存 provider 凭据（AES-GCM 加密存储到 plugin config）。"""
    from .crypto import encrypt
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip().lower()
    api_key = body.get("api_key", "")
    meta = _PROVIDER_META.get(provider)
    if meta is None:
        return _contract_error(f"unknown provider: {provider}", 400)
    if not meta["secret_key"]:
        return _contract_error(f"{provider} does not require an API key", 400)
    pm = current_app.extensions.get("plugin_manager")
    if pm is None or not pm.is_enabled("stock_analysis"):
        return _contract_error("plugin not enabled", 500)
    encrypted = encrypt(api_key) if api_key else ""
    pm.update_config("stock_analysis", {meta["secret_key"]: encrypted})
    return jsonify({"ok": True, "data": {"provider": provider, "saved": True},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/providers/test")
@_perm_required("stock.admin", "providers_test", limit=10, window=60.0)
def providers_test_connection():
    """测试 provider 连接（调用 health() 探测）。"""
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip().lower()
    if provider not in _PROVIDER_META:
        return _contract_error(f"unknown provider: {provider}", 400)
    try:
        from .providers.base_v2 import SecretResolver
        pm = current_app.extensions.get("plugin_manager")
        config = pm.get_config("stock_analysis") if pm and pm.is_enabled("stock_analysis") else {}
        meta = _PROVIDER_META[provider]
        if meta["secret_key"]:
            from .crypto import decrypt
            raw_key = decrypt(config.get(meta["secret_key"], ""))
        else:
            raw_key = None
        resolver = SecretResolver(lambda n: raw_key if n else None)
        if provider == "fmp":
            from .providers.fmp_provider import FMPProvider
            p = FMPProvider(secrets=resolver)
        elif provider == "polygon":
            from .providers.polygon_provider import PolygonProvider
            p = PolygonProvider(secrets=resolver)
        elif provider == "tushare":
            from .providers.tushare_provider import TushareProvider
            p = TushareProvider(secrets=resolver)
        elif provider in ("wind", "choice"):
            from .providers.terminal_provider import WindProvider, ChoiceProvider
            p = (WindProvider() if provider == "wind" else ChoiceProvider())
        else:
            return _contract_error(f"{provider} does not support health check", 400)
        result = p.health()
    except Exception as error:
        _LOGGER.error("provider test failed provider=%s: %s", provider, error)
        return _contract_error(f"test failed: {error}", 500)
    return jsonify({"ok": True, "data": {"provider": provider, "health": result},
                    "error": None, "meta": _generated_meta()})


# ── 桌面设置页：数据源凭据 / 指数选择（金融版方案；走插件持久化配置）──

def _plugin_config() -> dict:
    """读取 stock_analysis 插件持久化配置；无上下文/未启用返回 {}。"""
    try:
        pm = current_app.extensions.get("plugin_manager")
        if pm is not None and pm.is_enabled("stock_analysis"):
            return pm.get_config("stock_analysis") or {}
    except Exception:
        pass
    return {}


def _save_plugin_config(patch: dict) -> bool:
    """逐键写入插件持久化配置；插件不可用返回 False。"""
    pm = current_app.extensions.get("plugin_manager")
    if pm is None or not pm.is_enabled("stock_analysis"):
        return False
    for key, value in patch.items():
        if not pm.set_config("stock_analysis", key, value):
            return False
    return True


_DATA_SOURCE_KEYS = (("tushare", "tushare_token"),
                     ("fmp", "fmp_api_key"),
                     ("polygon", "polygon_api_key"))


@stock_analysis_bp.get("/api/settings/data-sources")
@_perm_required("stock.admin", "settings_data_sources", limit=30, window=60.0)
def settings_data_sources_get():
    """当前各数据源凭据状态（脱敏回显：configured + masked 尾段，绝不返回明文）。"""
    from .crypto import decrypt as _decrypt
    from .providers.base_v2 import SecretResolver
    cfg = _plugin_config()
    sources = []
    for provider, key in _DATA_SOURCE_KEYS:
        raw = cfg.get(key, "") or ""
        plain = _decrypt(str(raw)) if raw else ""
        sources.append({
            "provider": provider,
            "configured": bool(plain),
            "masked": SecretResolver.mask(plain) if plain else "",
        })
    return jsonify({"ok": True, "data": {"sources": sources},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/settings/data-sources")
@_perm_required("stock.admin", "settings_data_sources_save", limit=10, window=60.0)
def settings_data_sources_save():
    """保存数据源凭据（走 pm.set_config；与 tushare_client 三档解析的明文读取对齐）。

    password 字段允许为空 = 不修改；密钥绝不进日志、绝不回显明文。
    """
    body = request.get_json(silent=True) or {}
    patch = {}
    for provider, key in _DATA_SOURCE_KEYS:
        val = body.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or not val.strip():
            continue                      # 空 = 不修改
        patch[key] = val.strip()
    if not patch:
        return jsonify({"ok": True, "data": {"saved": False, "updated": []},
                        "error": None, "meta": _generated_meta()})
    if not _save_plugin_config(patch):
        return _contract_error("plugin not enabled", 500)
    return jsonify({"ok": True, "data": {"saved": True, "updated": list(patch.keys())},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/settings/data-sources/test")
@_perm_required("stock.admin", "settings_data_sources_test", limit=10, window=60.0)
def settings_data_sources_test():
    """测试数据源连接：tushare 走积分探针，fmp/polygon 走 provider health()。"""
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").strip().lower()
    if provider not in ("tushare", "fmp", "polygon"):
        return _contract_error("provider must be tushare|fmp|polygon", 400)
    try:
        if provider == "tushare":
            from . import tushare_client as tsc
            try:
                tsc.resolve_token()
                configured = True
            except Exception:
                configured = False
            caps = tsc.probe_capabilities()          # 失败内部缓存空集，不抛
            ok = bool(configured and caps)
            detail = {"configured": configured, "capabilities": sorted(caps)}
        else:
            from .crypto import decrypt as _decrypt
            from .providers.base_v2 import SecretResolver
            key_name = "fmp_api_key" if provider == "fmp" else "polygon_api_key"
            cfg = _plugin_config()
            raw = cfg.get(key_name, "") or ""
            plain = _decrypt(str(raw)) if raw else ""
            resolver = SecretResolver.from_plugin_config({key_name: plain})
            if provider == "fmp":
                from .providers.fmp_provider import FMPProvider
                p = FMPProvider(secrets=resolver)
            else:
                from .providers.polygon_provider import PolygonProvider
                p = PolygonProvider(secrets=resolver)
            health = p.health()
            ok = bool(health.get("ok"))
            detail = {"configured": bool(plain), "health": health}
    except Exception as error:
        _LOGGER.warning("data source test failed provider=%s: %s", provider, error)
        return jsonify({"ok": True, "data": {"provider": provider, "ok": False,
                                             "detail": {"error": str(error)}},
                        "error": None, "meta": _generated_meta()})
    return jsonify({"ok": True, "data": {"provider": provider, "ok": ok, "detail": detail},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/settings/indices")
@_perm_required("stock.read", "settings_indices", limit=30, window=60.0)
def settings_indices_get():
    """当前 index_selection + 候选清单（INDEX_CANDIDATES，供设置页多选）。"""
    from .providers.commons import INDEX_CANDIDATES
    cfg = _plugin_config()
    selection = cfg.get("index_selection") or ["sh000001", "sz399001", "sz399006"]
    return jsonify({"ok": True, "data": {"selection": selection,
                                         "candidates": INDEX_CANDIDATES},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.post("/api/settings/indices")
@_perm_required("stock.write", "settings_indices_save", limit=10, window=60.0)
def settings_indices_save():
    """保存 index_selection（逐只 index_symbol() 规范化为小写，候选表外符号也允许）。"""
    from .providers.commons import index_symbol
    body = request.get_json(silent=True) or {}
    raw_selection = body.get("selection")
    if not isinstance(raw_selection, list) or not raw_selection:
        return _contract_error("selection must be a non-empty array", 400)
    clean = []
    for s in raw_selection:
        sym = str(s).strip().lower()
        if not sym:
            continue
        clean.append(index_symbol(sym))
    if not clean:
        return _contract_error("selection must be a non-empty array", 400)
    if not _save_plugin_config({"index_selection": clean}):
        return _contract_error("plugin not enabled", 500)
    return jsonify({"ok": True, "data": {"saved": True, "selection": clean},
                    "error": None, "meta": _generated_meta()})


# ── 回测实验室（P1-W10）──

@stock_analysis_bp.post("/api/backtest")
@_perm_required("stock.write", "backtest_run", limit=10, window=60.0)
def backtest_run():
    """分层回测：传入因子名 + 标的池，返回 IC 报告 + 分层绩效。"""
    body = request.get_json(silent=True) or {}
    factor = (body.get("factor") or "").strip()
    universe = body.get("universe")
    if not factor:
        return _contract_error("factor is required", 400)
    if not isinstance(universe, list) or not universe:
        return _contract_error("universe must be a non-empty list", 400)
    datalen = max(60, min(body.get("datalen", 250), 1000))
    n_groups = max(2, min(body.get("n_groups", 5), 10))
    try:
        from .backtest_runner import BacktestRunner
        runner = BacktestRunner()
        result = runner.run_quintile(universe=universe, factor_name=factor,
                                     datalen=datalen, n_groups=n_groups)
    except Exception as error:
        _LOGGER.error("backtest failed factor=%s: %s", factor, error)
        return _contract_error("backtest run failed", 500)
    return _contract_result(result)


@stock_analysis_bp.get("/api/backtest/factors")
@_perm_required("stock.read", "backtest_factors")
def backtest_factors():
    """回测因子清单（只读下发，壳层据此渲染下拉，不再各自镜像）。

    事实来源为引擎注册表本身，端点内不二次硬编码：
      - `factor_lab.FACTOR_REGISTRY`（factor_lab.py:112）技术面 15 个 ——
        `/api/backtest` 实际可跑的因子（backtest_runner.py:306/:370 以该注册表判
        "unknown factor"），故 runnable=True。
      - `factor_lab.FUNDAMENTAL_FACTOR_REGISTRY`（factor_lab.py:201）基本面 13 个 ——
        已注册但当前无调用方（`/api/backtest` 会以 "unknown factor" 拒绝），
        如实下发并标 registry/runnable，避免壳层把它当成可跑因子。
      - `required_data`：`backtest_runner._FACTOR_REQUIRES`（backtest_runner.py:50）——
        声明缺失数据即 fail-closed 拒绝（backtest_runner.py:309/:374），属契约字段。
    分组/中文标签内核无事实来源（注册表只有因子名），故不下发 group/label。
    """
    from .backtest_runner import _FACTOR_REQUIRES
    from .factor_lab import FACTOR_REGISTRY, FUNDAMENTAL_FACTOR_REGISTRY

    factors = []
    for registry, runnable, table in (
            ("technical", True, FACTOR_REGISTRY),
            ("fundamental", False, FUNDAMENTAL_FACTOR_REGISTRY)):
        for name in table:
            factors.append({
                "name": name,
                "registry": registry,
                "runnable": runnable,
                "required_data": list(_FACTOR_REQUIRES.get(name, ())),
            })
    return jsonify({"ok": True, "data": {"factors": factors},
                    "error": None, "meta": _generated_meta()})


# ── 组合分析（P2-W18）──

@stock_analysis_bp.post("/api/portfolio/analyze")
@_perm_required("stock.write", "portfolio_analyze", limit=10, window=60.0)
def portfolio_analyze():
    """组合分析报告：持仓导入 → 行业暴露 + 集中度 + Beta/VaR + Brinson 归因。

    body: {
        holdings: [{symbol, weight} | {symbol, shares, price}],
        benchmark?: "000300",
        datalen?: 250
    }
    """
    body = request.get_json(silent=True) or {}
    raw_holdings = body.get("holdings")
    if not isinstance(raw_holdings, list) or not raw_holdings:
        return _contract_error("holdings must be a non-empty list", 400)
    try:
        from .portfolio import Portfolio, parse_holdings, full_report
        from .gateway import gateway
        holdings = parse_holdings(raw_holdings)
        if not holdings:
            return _contract_error("no valid holdings parsed", 400)
        pf = Portfolio(
            name=body.get("name", ""),
            holdings=holdings,
            benchmark=body.get("benchmark", ""),
        )
        datalen = max(60, min(body.get("datalen", 250), 500))
        result = full_report(pf, gateway, datalen=datalen)
    except Exception as error:
        _LOGGER.error("portfolio analyze failed: %s", error)
        return _contract_error("portfolio analysis failed", 500)
    return _contract_result(result)


# ── 合规与审计（P2-W19-20）──

@stock_analysis_bp.get("/api/compliance/audit")
@_perm_required("stock.read", "compliance_audit", limit=30, window=60.0)
def compliance_audit_list():
    """查询审计日志。query: symbol?, who?, limit?"""
    from . import models_sa as db
    symbol = request.args.get("symbol")
    who = request.args.get("who")
    limit = min(int(request.args.get("limit", 100)), 500)
    logs = db.list_audit_logs(symbol=symbol, who=who, limit=limit)
    return jsonify({"ok": True, "data": logs, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/compliance/audit/export")
@_perm_required("stock.admin", "compliance_audit_export", limit=5, window=60.0)
def compliance_audit_export():
    """导出审计日志（CSV/JSON）。query: format?(csv|json), symbol?, who?, from?, to?"""
    import csv, io
    from datetime import datetime
    from . import models_sa as db

    fmt = request.args.get("format", "json").lower()
    symbol = request.args.get("symbol")
    who = request.args.get("who")
    date_from = request.args.get("from")  # YYYY-MM-DD
    date_to = request.args.get("to")      # YYYY-MM-DD
    limit = min(int(request.args.get("limit", 5000)), 10000)

    logs = db.list_audit_logs(symbol=symbol, who=who, limit=limit,
                              date_from=date_from, date_to=date_to)

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=[
            "id", "who", "symbol", "action", "evidence_hash", "model",
            "prompt_version", "indicator_version", "cost", "created_at"
        ])
        writer.writeheader()
        for row in logs:
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
        return output.getvalue(), 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f"attachment; filename=audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        }
    return jsonify({"ok": True, "data": logs, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/compliance/check")
@_perm_required("stock.write", "compliance_check", limit=20, window=60.0)
def compliance_check():
    """对分析结果执行合规检查（适当性 + 静默期 + 利益冲突 + 留痕）。

    body: {
        symbol, signal?, confidence?, tags?, data_sources?, indicators?, model?,
        user_id?, suitability_level?, prompt_version?, indicator_version?
    }
    """
    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol")
    if not symbol:
        return _contract_error("symbol is required", 400)
    try:
        from .compliance import run_compliance_check, log_analysis
        result = {
            "symbol": symbol,
            "signal": body.get("signal"),
            "confidence": body.get("confidence"),
            "tags": body.get("tags") or [],
            "data_sources": body.get("data_sources") or [],
            "indicators": body.get("indicators") or [],
            "model": body.get("model"),
            "prompt_version": body.get("prompt_version"),
            "indicator_version": body.get("indicator_version"),
        }
        user_context = {
            "user_id": body.get("user_id", "anonymous"),
            "suitability_level": body.get("suitability_level", "normal"),
        }
        check_result = run_compliance_check(result, user_context)
        log_analysis(
            who=user_context["user_id"],
            symbol=symbol,
            action="compliance_check",
            evidence_hash=check_result["evidence_hash"],
            model=body.get("model"),
            prompt_version=body.get("prompt_version"),
            indicator_version=body.get("indicator_version"),
            result_summary={"passed": check_result["passed"],
                            "flags_count": len(check_result["flags"])},
        )
    except Exception as error:
        _LOGGER.error("compliance check failed: %s", error)
        return _contract_error("compliance check failed", 500)
    return jsonify({"ok": True, "data": check_result, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/compliance/approvals")
@_perm_required("stock.read", "compliance_approvals_list", limit=30, window=60.0)
def compliance_approvals_list():
    """查询审批列表。query: status? (draft/submitted/published/rejected), limit?"""
    from . import models_sa as db
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 50)), 200)
    approvals = db.list_approvals(status=status, limit=limit)
    return jsonify({"ok": True, "data": approvals, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/compliance/approvals/<int:approval_id>")
@_perm_required("stock.read", "compliance_approvals_get", limit=30, window=60.0)
def compliance_approvals_get(approval_id):
    """查询单条审批详情。"""
    from . import models_sa as db
    approval = db.get_approval(approval_id)
    if not approval:
        return _contract_error("approval not found", 404)
    return jsonify({"ok": True, "data": approval, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/compliance/approvals")
@_perm_required("stock.write", "compliance_approvals_create", limit=10, window=60.0)
def compliance_approvals_create():
    """创建并提交审批。body: {result_type, payload, title?, result_id?}"""
    body = request.get_json(silent=True) or {}
    result_type = body.get("result_type")
    payload = body.get("payload")
    if not result_type or payload is None:
        return _contract_error("result_type and payload are required", 400)
    try:
        from .compliance import submit_for_approval
        user_id = body.get("submitter", "anonymous")
        approval_id = submit_for_approval(
            result_type=result_type,
            payload=payload,
            title=body.get("title"),
            result_id=body.get("result_id"),
            submitter=user_id,
        )
    except Exception as error:
        _LOGGER.error("approval creation failed: %s", error)
        return _contract_error("approval creation failed", 500)
    return jsonify({"ok": True, "data": {"id": approval_id}, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/compliance/approvals/<int:approval_id>/review")
@_perm_required("stock.write", "compliance_approvals_review", limit=10, window=60.0)
def compliance_approvals_review(approval_id):
    """复核审批。body: {approved: bool, reviewer?, note?}"""
    body = request.get_json(silent=True) or {}
    approved = body.get("approved")
    if approved is None:
        return _contract_error("approved (bool) is required", 400)
    try:
        from . import models_sa as db
        reviewer = body.get("reviewer", "anonymous")
        note = body.get("note")
        ok = db.review_approval(approval_id, reviewer, bool(approved), note)
        if not ok:
            return _contract_error("approval not in submitted state or not found", 409)
    except Exception as error:
        _LOGGER.error("approval review failed: %s", error)
        return _contract_error("approval review failed", 500)
    return jsonify({"ok": True, "data": {"id": approval_id,
                                          "status": "published" if approved else "rejected"},
                    "error": None, "meta": _generated_meta()})


@stock_analysis_bp.get("/api/compliance/silence")
@_perm_required("stock.read", "compliance_silence_list", limit=30, window=60.0)
def compliance_silence_list():
    """查询静默期配置。query: user_id?"""
    from . import models_sa as db
    user_id = request.args.get("user_id")
    silences = db.list_silences(user_id=user_id)
    return jsonify({"ok": True, "data": silences, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/compliance/silence")
@_perm_required("stock.write", "compliance_silence_upsert", limit=10, window=60.0)
def compliance_silence_upsert():
    """登记/更新静默期配置。body: {user_id, symbol, position_date, days_before?, days_after?}"""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    symbol = body.get("symbol")
    position_date = body.get("position_date")
    if not user_id or not symbol or not position_date:
        return _contract_error("user_id, symbol, and position_date are required", 400)
    try:
        from . import models_sa as db
        db.upsert_silence(
            user_id=user_id, symbol=symbol, position_date=position_date,
            days_before=body.get("days_before", 1),
            days_after=body.get("days_after", 1),
        )
    except Exception as error:
        _LOGGER.error("silence upsert failed: %s", error)
        return _contract_error("silence upsert failed", 500)
    return jsonify({"ok": True, "data": {"user_id": user_id, "symbol": symbol,
                                          "position_date": position_date},
                    "error": None, "meta": _generated_meta()})


# ── 研报知识库（P2-W20）──

@stock_analysis_bp.get("/api/kb/docs")
@_perm_required("stock.read", "kb_list", limit=30, window=60.0)
def kb_list_docs():
    """列出研报文档。query: keyword?, symbol?, status?, limit?, offset?"""
    keyword = request.args.get("keyword", "")
    symbol = request.args.get("symbol", "")
    status = request.args.get("status", "")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    try:
        from . import kb
        docs = kb.list_research_docs(keyword=keyword, symbol=symbol,
                                     status=status, limit=limit, offset=offset)
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("list")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb list failed: %s", error)
        return _contract_error("kb list failed", 500)
    return jsonify({"ok": True, "data": docs, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/kb/upload")
@_perm_required("stock.write", "kb_upload", limit=10, window=60.0)
def kb_upload():
    """上传研报 PDF。form: file, symbol?, report_type?, analyst?

    委托 project_workspace 处理 PDF 抽取/分块/嵌入，附加股票元数据。
    """
    if "file" not in request.files:
        return _contract_error("file is required", 400)
    f = request.files["file"]
    if not f.filename:
        return _contract_error("empty filename", 400)
    import os
    import uuid as _uuid
    ext = os.path.splitext(f.filename)[1].lower()
    allowed = {".pdf", ".docx", ".txt", ".md", ".pptx"}
    if ext not in allowed:
        return _contract_error("unsupported file type: %s" % ext, 400)
    f.seek(0, 2)
    file_size = f.tell()
    f.seek(0)
    max_size = 50 * 1024 * 1024
    if file_size > max_size:
        return _contract_error("file too large (max 50MB)", 400)
    try:
        from . import kb
        pw_get_db = kb.pw_get_db                      # 软依赖：后端缺失即抛 KnowledgeBaseUnavailable
        _DocProcessor, resolve_storage_dir = kb.pw_doc_processor()  # DocProcessor 由后台任务内再取
        user_id = str(request._jwt_user.get("user_id", "")) if hasattr(request, "_jwt_user") else "system"
        project_id = kb.ensure_stock_research_project(user_id)
        doc_id = str(_uuid.uuid4())
        safe_name = doc_id + ext
        pm = current_app.extensions.get("plugin_manager")
        pw_config = {}
        if pm:
            pw_config = pm.get_config("project_workspace") or {}
        storage_dir = resolve_storage_dir(pw_config)
        os.makedirs(storage_dir, exist_ok=True)
        filepath = os.path.join(storage_dir, safe_name)
        f.save(filepath)
        metadata = {
            "source": "stock_analysis",
            "symbol": request.form.get("symbol", ""),
            "report_type": request.form.get("report_type", ""),
            "analyst": request.form.get("analyst", ""),
        }
        conn = pw_get_db()
        try:
            conn.execute(
                "INSERT INTO documents"
                " (id, project_id, filename, original_name, file_ext, file_size,"
                "  mime_type, status, uploaded_by, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?::jsonb)",
                (doc_id, project_id, safe_name, f.filename, ext.lstrip("."),
                 file_size, f.content_type or "", user_id,
                 json.dumps(metadata, ensure_ascii=False))
            )
            conn.commit()
        finally:
            conn.close()
        worker = current_app.config.get("AUTOMATION_WORKER")
        if worker is None:
            _process_kb_doc(doc_id, project_id, filepath, f.filename, pw_config)
        else:
            worker.submit_task(
                task_type="python",
                task_data={
                    "func": _process_kb_doc,
                    "kwargs": {
                        "doc_id": doc_id, "project_id": project_id,
                        "filepath": filepath, "filename": f.filename,
                        "config": pw_config,
                    },
                },
                priority="NORMAL",
                task_id="sa_kb_%s" % doc_id,
            )
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("upload")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb upload failed: %s", error)
        return _contract_error("kb upload failed", 500)
    return jsonify({"ok": True, "data": {"document_id": doc_id, "status": "pending"},
                    "error": None, "meta": _generated_meta()})


def _process_kb_doc(doc_id, project_id, filepath, filename, config):
    """异步处理研报文档：抽取 → 分块 → 嵌入 → 存储。"""
    from . import kb
    conn = kb.pw_get_db()                # 软依赖：后端被裁时抛 KnowledgeBaseUnavailable（可读降级原因）
    DocProcessor, _resolve_storage_dir = kb.pw_doc_processor()
    try:
        processor = DocProcessor(config)
        processor.process_document(doc_id, project_id, filepath, filename, conn)
        conn.execute(
            "UPDATE projects SET doc_count = ("
            " SELECT COUNT(*) FROM documents WHERE project_id = ? AND status = 'ready')"
            " WHERE id = ?",
            (project_id, project_id)
        )
        conn.commit()
    except Exception as e:
        _LOGGER.error("kb doc processing failed: %s", e)
        try:
            conn.rollback()
            conn.execute(
                "UPDATE documents SET status = 'failed', error_msg = ?"
                " WHERE id = ?", (str(e)[:500], doc_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


@stock_analysis_bp.get("/api/kb/docs/<doc_id>/status")
@_perm_required("stock.read", "kb_status", limit=60, window=60.0)
def kb_doc_status(doc_id):
    """查询研报处理状态。"""
    try:
        from . import kb
        status = kb.get_doc_status(doc_id)
        if not status:
            return _contract_error("document not found", 404)
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("status")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb status failed: %s", error)
        return _contract_error("kb status failed", 500)
    return jsonify({"ok": True, "data": status, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.delete("/api/kb/docs/<doc_id>")
@_perm_required("stock.write", "kb_delete", limit=10, window=60.0)
def kb_delete_doc(doc_id):
    """删除研报文档及其分块。"""
    try:
        from . import kb
        conn = kb.pw_get_db()            # 软依赖：后端缺失 → KnowledgeBaseUnavailable → 503
        try:
            conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            conn.commit()
        finally:
            conn.close()
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("delete")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb delete failed: %s", error)
        return _contract_error("kb delete failed", 500)
    return jsonify({"ok": True, "data": {"deleted": doc_id}, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/kb/search")
@_perm_required("stock.read", "kb_search", limit=30, window=60.0)
def kb_search():
    """研报语义检索。query: q, symbol?, top_k?"""
    query = request.args.get("q", "").strip()
    if not query:
        return _contract_error("q is required", 400)
    symbol = request.args.get("symbol", "")
    top_k = min(int(request.args.get("top_k", 10)), 50)
    user_id = ""
    if hasattr(request, "_jwt_user"):
        user_id = str(request._jwt_user.get("user_id", ""))
    try:
        from . import kb
        results = kb.search_research(query=query, symbol=symbol,
                                     top_k=top_k, user_id=user_id)
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("search")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb search failed: %s", error)
        return _contract_error("kb search failed", 500)
    return jsonify({"ok": True, "data": results, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/kb/qa")
@_perm_required("stock.read", "kb_qa", limit=10, window=60.0)
def kb_qa():
    """研报 Q&A。body: {query, top_k?}"""
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return _contract_error("query is required", 400)
    top_k = min(int(body.get("top_k", 5)), 20)
    user_id = ""
    if hasattr(request, "_jwt_user"):
        user_id = str(request._jwt_user.get("user_id", ""))
    try:
        from . import kb
        result = kb.qa_research(query=query, top_k=top_k, user_id=user_id)
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("qa")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb qa failed: %s", error)
        return _contract_error("kb qa failed", 500)
    return jsonify({"ok": True, "data": result, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/kb/stats")
@_perm_required("stock.read", "kb_stats", limit=60, window=60.0)
def kb_stats():
    """研报库统计。"""
    try:
        from . import kb
        stats = kb.get_kb_stats()
    except KnowledgeBaseUnavailable:
        return _kb_unavailable("stats")   # 依赖未装载（发行版门控）→ 503，不记 500 裸栈
    except Exception as error:
        _LOGGER.error("kb stats failed: %s", error)
        return _contract_error("kb stats failed", 500)
    return jsonify({"ok": True, "data": stats, "error": None,
                    "meta": _generated_meta()})


# ══════════════════════════════════════════════════════════════════
# 交易通道（方案 §4.7）—— 仿真默认，实盘 fail-closed
#
# 合规边界：本插件 v1 **只提供仿真账户**（不涉真实资金）。实盘适配器属可选插件，
# 需自备经纪商资质 + 法务评审；在此之前 `live=true` 一律 503，绝不静默退化成仿真
# （沉默降级会让用户误以为自己在真钱交易）。
#
# 所有下单**必须先过 RiskGate**，本文件不存在绕过闸门的下单路径。
# ══════════════════════════════════════════════════════════════════

def _trade_user_id() -> str:
    """取当前操作者（审计用）。取不到不阻塞下单，但审计里如实记 unknown。"""
    try:
        payload, auth_err = _require_perm("stock.read")
        if payload and not auth_err:
            return str(payload.get("sub") or payload.get("username") or "unknown")
    except Exception:                                   # noqa: BLE001
        pass
    return "unknown"


def _trade_quote(symbol: str):
    """取 (名称, 现价) 供风控闸门用；取不到返回 (None, None)。

    名称用于识别 ST/退市风险警示，现价用于估算金额与仓位占比。
    取不到时闸门会把它写进 warnings（"未执行"），**不假装校验过**。
    """
    try:
        from .gateway import gateway
        q = gateway.get_quote(symbol) or {}
        price = q.get("price")
        price = float(price) if price else None
        return (q.get("name") or None), (price or None)
    except Exception:                                   # noqa: BLE001
        return None, None


def _trade_audit(who: str, symbol, action: str, summary: dict) -> None:
    """下单审计留痕。失败不阻塞交易，但会记 error 日志（审计丢失是可观测事件）。"""
    try:
        from .compliance import log_analysis
        log_analysis(who=who, symbol=symbol, action=action, result_summary=summary)
    except Exception as error:                          # noqa: BLE001
        _LOGGER.error("trade audit failed action=%s: %s", action, error)


@stock_analysis_bp.get("/api/trade/account")
@_perm_required("stock.read", "trade_account", limit=60, window=60.0)
def trade_account():
    """仿真账户概览 + 当前通道信息（是否仿真）。

    前端据此打「仿真」角标 —— 角标数据来自后端 `describe()`，不由前端自行判断。
    """
    try:
        from .broker import get_adapter
        adapter = get_adapter(user_id=_trade_user_id())
        data = adapter.account()
        data["channel"] = adapter.describe()
    except Exception as error:
        _LOGGER.error("trade account failed: %s", error)
        return _contract_error("trade account unavailable", 503)
    return jsonify({"ok": True, "data": data, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/trade/positions")
@_perm_required("stock.read", "trade_positions", limit=60, window=60.0)
def trade_positions():
    """仿真持仓（含现价、市值、浮盈亏、T+1 锁定标记）。"""
    try:
        from .broker import get_adapter
        rows = get_adapter(user_id=_trade_user_id()).positions()
    except Exception as error:
        _LOGGER.error("trade positions failed: %s", error)
        return _contract_error("positions unavailable", 503)
    return jsonify({"ok": True, "data": {"positions": rows}, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.get("/api/trade/orders")
@_perm_required("stock.read", "trade_orders", limit=60, window=60.0)
def trade_orders():
    """仿真订单流水（新→旧）。被拒单也在列，审计要能看到"想干但没干成"的尝试。"""
    status = request.args.get("status") or None
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    try:
        from .broker import get_adapter
        rows = get_adapter(user_id=_trade_user_id()).orders(status=status, limit=limit)
    except Exception as error:
        _LOGGER.error("trade orders failed: %s", error)
        return _contract_error("orders unavailable", 503)
    return jsonify({"ok": True, "data": {"orders": rows}, "error": None,
                    "meta": _generated_meta()})


@stock_analysis_bp.post("/api/trade/order")
@_perm_required("stock.write", "trade_order", limit=5, window=60.0)
def trade_order():
    """下单（**默认仿真**）。

    流程：取行情（名称+现价）→ 组装账户快照 → **RiskGate 过闸** → 成交。
    三种非 200 结果：
      * 400 参数/业务错误（含持仓不足、限价不可成交等）
      * 403 风控硬拒绝（闸门不放行）
      * 409 **需二次确认**（高风险标的），meta.needsConfirm=true，前端确认后带 confirm=true 重提
      * 503 通道不可用（含 live=true 但无实盘实现）
    与方案示例的差异：方案把"需确认"也归到 403，这里用 409 区分，
    否则前端只能靠解析 error 文案判断要不要弹确认框。
    """
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").strip()
    if not symbol:
        return _contract_error("symbol is required", 400)

    user_id = _trade_user_id()
    try:
        from .broker import (AdapterError, AdapterUnavailable, RiskGate,
                             get_adapter)
        from .broker.adapter import BrokerAdapter

        adapter = get_adapter(user_id=user_id, live=bool(body.get("live")))
        norm = BrokerAdapter._norm_symbol(symbol)
        name, last = _trade_quote(norm)

        acct = adapter.account()
        held = next((p for p in adapter.positions() if p["symbol"] == norm), None)

        verdict = RiskGate.check(
            body,
            account={
                "cash": acct.get("cash"),
                "total": acct.get("total"),
                "lastPrice": last,
                "heldQty": (held or {}).get("qty"),
                "heldMarketValue": (held or {}).get("marketValue"),
            },
            name=name, user_id=user_id, confirm=bool(body.get("confirm")),
        )

        if verdict.needs_confirm:
            _trade_audit(user_id, norm, "trade_order_need_confirm",
                         {"reason": verdict.reason, "raw": body})
            return (jsonify({"ok": False, "data": None, "error": verdict.reason,
                             "meta": {"needsConfirm": True,
                                      "warnings": verdict.warnings}}), 409)
        if not verdict.ok:
            _trade_audit(user_id, norm, "trade_order_blocked",
                         {"reason": verdict.reason, "raw": body})
            return _contract_error(verdict.reason, 403)

        result = adapter.place_order(
            symbol=norm,
            side=str(body.get("side") or "").strip().lower(),
            qty=body.get("qty"),
            price=body.get("price"),
            order_type=str(body.get("type") or body.get("order_type") or "limit")
                        .strip().lower(),
        )
        _trade_audit(user_id, norm, "trade_order",
                     {"orderId": result.get("orderId"),
                      "status": result.get("status"),
                      "qty": result.get("qty"),
                      "amount": result.get("amount")})
        meta = _generated_meta()
        meta["warnings"] = verdict.warnings
        meta["paper"] = True
        return jsonify({"ok": True, "data": result, "error": None, "meta": meta})

    except AdapterUnavailable as error:
        # 通道不存在（如 live=true 但无实盘实现）→ 503，不是 400
        _LOGGER.warning("trade channel unavailable: %s", error)
        return _contract_error(str(error), 503)
    except AdapterError as error:
        _trade_audit(user_id, symbol, "trade_order_rejected",
                     {"reason": str(error)[:200], "raw": body})
        return _contract_error(str(error), 400)
    except Exception as error:
        _LOGGER.error("trade order failed: %s", error)
        return _contract_error("trade order failed", 500)


@stock_analysis_bp.post("/api/trade/reset")
@_perm_required("stock.admin", "trade_reset", limit=5, window=60.0)
def trade_reset():
    """复位仿真账本（破坏性操作，管理员权限）。

    归属说明（2026-09-21 架构整改）：sa_paper_* 三张账本表归本插件所有，复位
    必须经插件落库 —— 此前 scripts/trade-reset.py / broker-selftest.py 自行
    psycopg2 拼 DELETE 触碰账本（绕过适配器不变量），现收敛到
    `PaperAdapter.reset()` 并由本端点对外暴露。

    body：`{"all": true}` 清全部使用者，或 `{"user_id": "..."}` 清指定使用者，
    **二者必居其一**，都不给返回 400（避免误清整本账）。账本本身是仿真账本
    （LIVE=False）；若环境要求实盘通道，`get_adapter` 会 fail-closed 抛
    AdapterUnavailable → 503，本端点绝不清真实账。
    """
    body = request.get_json(silent=True) or {}
    if body.get("all") is True:
        target = None
    elif str(body.get("user_id") or "").strip():
        target = str(body["user_id"]).strip()
    else:
        return _contract_error("需指定 all=true 或 user_id（避免误清账本）", 400)
    # 在 try 外绑定：except 子句需要该名字在函数作用域可见
    from .broker import AdapterUnavailable, get_adapter
    try:
        adapter = get_adapter(user_id=_trade_user_id())
        purged = adapter.reset(target)
    except AdapterUnavailable as error:
        _LOGGER.warning("trade reset refused: %s", error)
        return _contract_error(str(error), 503)
    except Exception as error:                          # noqa: BLE001
        _LOGGER.error("trade reset failed: %s", error)
        return _contract_error("trade reset failed", 500)
    _trade_audit(_trade_user_id(), None, "trade_reset",
                 {"scope": target or "all", "purged": purged})
    return jsonify({"ok": True, "data": {"purged": purged}, "error": None,
                    "meta": _generated_meta()})


# ── 本体层（Evolution Ring）── 方案 §2「认知进化/本体层」─────────────
#
# 现状（2026-09-17 实测）：
#   - 真后端是 **memory_engine** 插件（PG 的 memory_engine schema），不是 cogevolution_substrate。
#   - memory_engine 自带的 /admin/memory/graph **全部 @admin_required**：非管理员直接 401。
#     本路由不改 admin 端点权限、不加网关代理，而是沿用本插件已有的既定范式
#     （见 /api/agents/runtime）：**stock.read 权限点 + 跨插件直读 DB**。
#   - 数据当前为 0（memories/reflexion_logs/prompt_metrics/evolution_rounds 实测均空），
#     故 available=true 但 nodes 为空是**正常空态**，不是缺陷 —— 前端必须区分
#     "插件不可用"与"尚无进化记录"，不得用 mock 顶替。

_ONTOLOGY_MAX_MEMORIES = 200      # 与 memory_engine 原生 graph 一致
_ONTOLOGY_MAX_NODES = 60          # 出给 UI 的节点上限（原生实现会把整批 200 条都吐出来）
_ONTOLOGY_MAX_LINKS = 400
_ONTOLOGY_PER_AGENT_MEM = 15      # 单 agent 参与建边的记忆上限，避免 O(n²) 边爆炸


@stock_analysis_bp.get("/api/ontology/graph")
@_perm_required("stock.read", "ontology_graph", limit=30, window=60.0)
def ontology_graph():
    """本体层进化环：nodes + links（方案 §2）。

    与 memory_engine 原生 /admin/memory/graph 的三点差异（均为 UI 可用性所必需）：
      1. 权限用 stock.read，**不需要 admin token**；
      2. **先裁节点再建边** —— 原生实现在 200 条记忆上两两连边，同 agent 时最多产生 ~19900 条边，
         前端必卡死。此处按 importance 取 top N（默认 60），单 agent 参与连边上限 15；
      3. 额外返回 counts / available / truncated，让前端能说清"为什么是空的"。

    字段**原样透传** memory_engine 的命名（kind/phase/agent_id/importance...），不做重命名。
    """
    owner_type = request.args.get("owner_type", "user")
    owner_id = request.args.get("owner_id", "")
    round_id = request.args.get("round_id", "")
    max_nodes = request.args.get("max_nodes", _ONTOLOGY_MAX_NODES, type=int)
    max_nodes = max(10, min(max_nodes, 200))
    max_links = request.args.get("max_links", _ONTOLOGY_MAX_LINKS, type=int)
    max_links = max(20, min(max_links, 2000))

    empty = {
        "nodes": [], "links": [], "round": None,
        "counts": {"memories": 0, "reflexions": 0, "prompts": 0, "rounds": 0},
        "available": False, "asOf": None, "truncated": {}, "source": "memory_engine",
    }

    try:
        from plugins.memory_engine.models import get_memory_engine_db
    except Exception as error:
        _LOGGER.warning("memory_engine plugin unavailable: %s", error)
        empty["meta_note"] = "memory_engine plugin not importable"
        return jsonify({"ok": True, "data": empty, "error": None,
                        "meta": _generated_meta()})

    try:
        conn = get_memory_engine_db()
    except Exception as error:
        _LOGGER.warning("memory_engine db unavailable: %s", error)
        empty["meta_note"] = "memory_engine db unreachable"
        return jsonify({"ok": True, "data": empty, "error": None,
                        "meta": _generated_meta()})

    try:
        rounds = _me_count(conn, "evolution_rounds")
        total_mem = _me_count(conn, "memories")
        total_ref = _me_count(conn, "reflexion_logs")
        total_prm = _me_count(conn, "prompt_metrics")

        win = None
        try:
            if round_id:
                win = conn.execute(
                    "SELECT id, agent_id, window_start, window_end FROM evolution_rounds"
                    " WHERE id = ?", (round_id,)).fetchone()
            else:
                win = conn.execute(
                    "SELECT id, agent_id, window_start, window_end FROM evolution_rounds"
                    " WHERE status = 'closed' ORDER BY window_start DESC LIMIT 1").fetchone()
        except Exception as error:
            _LOGGER.warning("ontology round lookup failed: %s", error)

        since = win["window_start"] if win else None
        until = win["window_end"] if win else None

        mem_nodes = []
        try:
            sql = ("SELECT id, agent_id, memory_type, content, importance, quality_score"
                   " FROM memories WHERE owner_type = ? AND owner_id = ? AND status = 'active'")
            args = [owner_type, owner_id]
            if since:
                sql += " AND created_at >= ?"; args.append(since)
            if until:
                sql += " AND created_at < ?"; args.append(until)
            sql += " ORDER BY importance DESC LIMIT ?"
            args.append(_ONTOLOGY_MAX_MEMORIES)
            for r in conn.execute(sql, args).fetchall():
                mem_nodes.append({
                    "id": str(r["id"]),
                    "kind": "memory",
                    "phase": "experience" if r["memory_type"] == "lesson" else "mem_extract",
                    "agent_id": r["agent_id"],
                    "content": str(r["content"])[:120],
                    "importance": float(r["importance"] or 0.5),
                    "quality_score": float(r["quality_score"] or 0.5),
                })
        except Exception as error:
            _LOGGER.warning("ontology memories failed: %s", error)

        ref_nodes = []
        try:
            rsql = "SELECT id, agent_id, issue, lesson FROM reflexion_logs WHERE 1=1"
            rargs = []
            if since:
                rsql += " AND created_at >= ?"; rargs.append(since)
            if until:
                rsql += " AND created_at < ?"; rargs.append(until)
            rsql += " ORDER BY created_at DESC LIMIT 100"
            for r in conn.execute(rsql, rargs).fetchall():
                ref_nodes.append({
                    "id": "ref_" + str(r["id"]),
                    "kind": "reflexion", "phase": "reflexion",
                    "agent_id": r["agent_id"],
                    "content": str(r["lesson"] or r["issue"] or "")[:120],
                    "importance": 0.5,
                })
        except Exception as error:
            _LOGGER.warning("ontology reflexions failed: %s", error)

        prm_nodes = []
        try:
            for r in conn.execute(
                "SELECT DISTINCT ON (agent_id) agent_id, prompt_hash, prompt_version"
                " FROM prompt_metrics ORDER BY agent_id, updated_at DESC").fetchall():
                prm_nodes.append({
                    "id": "prm_" + str(r["prompt_hash"]),
                    "kind": "prompt", "phase": "prompt_evolve",
                    "agent_id": r["agent_id"],
                    "content": "v" + str(r["prompt_version"]),
                    "importance": 0.5,
                })
        except Exception as error:
            _LOGGER.warning("ontology prompts failed: %s", error)

        # 先裁节点（保 prompt/reflexion 节点不被 memories 挤掉，各留至少 1/3 配额）
        n_prm = min(len(prm_nodes), max(1, max_nodes // 4))
        n_ref = min(len(ref_nodes), max(1, max_nodes // 4))
        n_mem = max(0, max_nodes - n_prm - n_ref)
        kept_prm = prm_nodes[:n_prm]
        kept_ref = ref_nodes[:n_ref]
        kept_mem = mem_nodes[:n_mem]
        nodes = kept_mem + kept_ref + kept_prm

        total_nodes = len(mem_nodes) + len(ref_nodes) + len(prm_nodes)

        # 再建边（在已裁剪集合上；单 agent 参与建边的记忆数受限）
        links = []
        by_agent = {}
        for nd in nodes:
            if nd["kind"] == "memory":
                by_agent.setdefault(nd["agent_id"], []).append(nd["id"])
        for _ag, ids in by_agent.items():
            ids = ids[:_ONTOLOGY_PER_AGENT_MEM]
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    links.append({"source": ids[i], "target": ids[j],
                                  "relation": "same_agent"})
        mem_by_agent = by_agent
        for nd in nodes:
            if nd["kind"] not in ("reflexion", "prompt"):
                continue
            rel = "reflexed" if nd["kind"] == "reflexion" else "evolves"
            for mid in mem_by_agent.get(nd["agent_id"], [])[:5]:
                links.append({"source": nd["id"], "target": mid, "relation": rel})

        truncated = {}
        total_links = len(links)
        if total_links > max_links:
            links = links[:max_links]
            truncated["links"] = True
        if total_nodes > len(nodes):
            truncated["nodes"] = True

        data = {
            "nodes": nodes,
            "links": links,
            "round": dict(win) if win else None,
            "counts": {"memories": total_mem, "reflexions": total_ref,
                       "prompts": total_prm, "rounds": rounds},
            "available": True,
            "asOf": _generated_meta()["generated_at"],
            "truncated": truncated,
            "source": "memory_engine",
        }
        if owner_id == "":
            # 与原生 graph 同默认。owner 为空时很可能查不到任何记忆，明示而非静默返空。
            data["ownerNote"] = ("owner_id 为空 —— 与 memory_engine 原生 graph 默认值一致；"
                                 "若确无数据请先确认记忆写入时的 owner_type/owner_id")
        return jsonify({"ok": True, "data": data, "error": None,
                        "meta": _generated_meta()})
    except Exception as error:
        _LOGGER.warning("ontology graph failed: %s", error)
        empty["meta_note"] = "ontology graph failed"
        return jsonify({"ok": True, "data": empty, "error": None,
                        "meta": _generated_meta()})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 连接已断时不能再抛
            pass


def _me_count(conn, table: str) -> int:
    """表行数；表不存在/不可读返回 0（记忆库为空是可降级的正常态）。"""
    try:
        row = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()
        return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0
