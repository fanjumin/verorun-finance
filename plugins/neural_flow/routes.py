"""routes.py — NeuralFlow 基座端点（管理端口径：JWT is_admin，方案 §7.10.4）。

  GET /admin/neural-flow/api/profiles   档案注册表（含拒载留痕）
  GET /admin/neural-flow/api/spans      回放查询（nf_flow_spans，按 id 排序）
  GET /admin/neural-flow/api/events     SSE 实时通道（备用；finance 版默认走
                                        stock_analysis 单连接，见 sse_stream 模块注释）

鉴权范式与 stock_analysis/routes.py:52 _require_admin 同构（validate_token +
is_admin，401/403 分离）；不发新 RBAC 权限点（平台级管理端功能）。
"""
from __future__ import annotations

import os
import threading

from flask import Blueprint, Response, jsonify, request, stream_with_context
from plugin_manager.logger import get_plugin_logger

_log = get_plugin_logger("neural_flow")

neural_flow_bp = Blueprint(
    "neural_flow",
    __name__,
    url_prefix="/admin/neural-flow",
)

# ── SSE 长连接并发闸（与 stock_analysis DEF-02 同构）──
# 每条 SSE 独占一个 worker；闸位放大会重现「SSE 拖垮管理端」。非法值只告警回退，
# 绝不在 import 期抛异常（否则插件 setup 直接失败）。
_SSE_LIMIT_ENV = "NF_SSE_MAX_CONNECTIONS"
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
        _log.warning("%s=%r 非整数，回退默认 %d", _SSE_LIMIT_ENV, raw, _SSE_LIMIT_DEFAULT)
        return _SSE_LIMIT_DEFAULT
    clamped = max(_SSE_LIMIT_MIN, min(n, _SSE_LIMIT_MAX))
    if clamped != n:
        _log.warning("%s=%d 越界，钳制为 %d（允许区间 %d~%d）",
                     _SSE_LIMIT_ENV, n, clamped, _SSE_LIMIT_MIN, _SSE_LIMIT_MAX)
    return clamped


_SSE_MAX_CONNECTIONS = _resolve_sse_max_connections()
_sse_slots = threading.BoundedSemaphore(_SSE_MAX_CONNECTIONS)


def _require_admin():
    """返回 (payload, error)：payload None → 401；error 非空 → 403。"""
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


def _admin_guard():
    payload, err = _require_admin()
    if payload is None:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if err:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


@neural_flow_bp.get("/api/profiles")
def profiles():
    """Domain Profile 注册表（桌面端 Profile 驱动渲染的数据源）。"""
    guard = _admin_guard()
    if guard:
        return guard
    from .profile_registry import list_profiles, get_errors
    return jsonify({
        "ok": True,
        "data": {"profiles": list_profiles(),
                 "rejected": get_errors()},
        "error": None,
    })


@neural_flow_bp.get("/api/spans")
def spans():
    """回放查询（nf_flow_spans 归档，30 天窗口；排序按 id——双时基纪律）。"""
    guard = _admin_guard()
    if guard:
        return guard
    from .models_nf import list_spans

    def _f(name):
        raw = request.args.get(name, "").strip()
        return float(raw) if raw else None

    try:
        rows = list_spans(
            from_ts=_f("from"), to_ts=_f("to"),
            domain=request.args.get("domain", "").strip() or None,
            trace_id=request.args.get("trace_id", "").strip() or None,
            limit=max(1, min(int(request.args.get("limit", "500") or 500), 2000)),
        )
    except ValueError:
        return jsonify({"ok": False, "error": "invalid numeric param"}), 400
    except Exception as err:
        _log.error("spans query failed: %s", err)
        return jsonify({"ok": False, "error": "spans unavailable"}), 503
    return jsonify({"ok": True, "data": {"items": rows, "count": len(rows)},
                    "error": None})


@neural_flow_bp.get("/api/events")
def events():
    """SSE 实时通道（topics=flow；Last-Event-ID 断线补发；并发闸 _SSE_MAX_CONNECTIONS）。

    鉴权口径与另两端点一致：未认证 401 / 非管理员 403（首次鉴权走 JSON）；
    闸满 → 503 + Retry-After: 10（不排队，避免拖垮管理端普通请求）；
    出流依赖不可用 → 503 + Retry-After: 60（建流前拒，不占闸位、不空转刷日志）。
    """
    payload, err = _require_admin()
    if payload is None:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if err:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    from . import sse_stream

    topics, topic_err = sse_stream.parse_topics(request.args.get("topics", ""))
    if topics is None:
        return jsonify({"ok": False, "error": topic_err}), 400

    last_id, last_id_err = sse_stream.parse_last_id(
        request.headers.get("Last-Event-ID", ""))
    if last_id_err:
        return jsonify({"ok": False, "error": last_id_err}), 400

    # 出流依赖不可用（stock_analysis 未装/未激活，或其 sa_sse_events 未建）时，
    # 在建流前明确降级为 503 —— 否则本端点会退化成「只发心跳 + 每秒一条同因告警」
    # 的无限空转流（2026-10-04 生产实测：单连接 20s 内 35 条 WARNING、0 业务帧）。
    # 放在取槽位之前：不可用的流不该占用任何闸位。
    outbox_err = sse_stream.probe_outbox()
    if outbox_err:
        _log.warning("sse realtime outbox unavailable: %s", outbox_err)
        resp = jsonify({"ok": False, "error": "realtime outbox unavailable"})
        resp.status_code = 503
        resp.headers["Retry-After"] = "60"
        return resp

    token = (request.headers.get("Authorization", "").replace("Bearer ", "")
             or request.cookies.get("sso_token") or request.headers.get("X-Token") or "")

    def _auth_check(tok):
        from services.jwt_service import validate_token
        try:
            p = validate_token(tok)
            return p if p and p.get("is_admin") else None
        except Exception:
            return None

    if not _sse_slots.acquire(blocking=False):
        _log.warning("sse slots full (%d), rejecting new stream", _SSE_MAX_CONNECTIONS)
        resp = jsonify({"ok": False, "error": "too many concurrent event streams"})
        resp.status_code = 503
        resp.headers["Retry-After"] = "10"
        return resp

    def _gen():
        try:
            yield from sse_stream.stream_events(topics, last_id, token, _auth_check)
        finally:
            _sse_slots.release()

    resp = Response(stream_with_context(_gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp
