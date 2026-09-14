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
    if analysis_type not in {"technical", "fundamental", "sentiment", "llm"}:
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
        return jsonify({"success": True, "data": rows})
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
    if kind not in {"technical", "fundamental", "sentiment", "llm"}:
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
    """深财报四表合一（Tushare income/balance/cashflow/fina_indicator，各 8 期）。无权限返回 404。"""
    symbol, sym_err = _symbol()
    if symbol is None:
        return jsonify({"success": False, "error": sym_err}), 400
    try:
        from .gateway import gateway
        data = gateway.get_fundamental(symbol)
    except Exception as error:
        _LOGGER.warning("fundamental unavailable symbol=%s: %s", symbol, error)
        return jsonify({"success": False,
                        "error": "fundamental unavailable (tushare not configured or no permission)"}), 404
    return jsonify({"success": True, "symbol": symbol, "data": data})


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
    return jsonify({"ok": True, "data": result, "error": None,
                    "meta": _generated_meta()})


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
    return jsonify({"ok": True, "data": result, "error": None,
                    "meta": _generated_meta()})


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
