#!/usr/bin/env python3
"""
VeroRun — 可观测性基础设施
============================
1. request_id / user_id contextvar 注入（请求级，贯通日志 JSON 与响应头）
2. 健康检查工具（live / ready 复用）

用法：
    from shared.observability import init_request_id, current_request_id, measure, build_health_payload

    # 请求入口（before_request）：
    init_request_id()

    # 日志：JsonFormatter 自动并入 request_id/user_id（见 shared.logging）
    logger.info('order created', extra={'extra_order_no': order_no})

    # 健康检查：
    checks = [measure(_pg_ok, 'pg'), measure(_mgr_ok, 'plugin_manager')]
    return jsonify(build_health_payload('platform', checks))
"""

import contextvars
import time
import uuid
from typing import Any, Dict, List, Optional

# ── 请求上下文（contextvars：多线程/异步安全） ──────────────────────

_request_id_var: contextvars.ContextVar = contextvars.ContextVar(
    'verorun_request_id', default='')
_user_id_var: contextvars.ContextVar = contextvars.ContextVar(
    'verorun_user_id', default='')


def init_request_id() -> str:
    """生成并注入当前请求的 request_id（uuid 短码前 8 位），并清空 user_id。

    在 Flask before_request 中调用；每个请求拥有唯一 id。
    """
    rid = uuid.uuid4().hex[:8]
    _request_id_var.set(rid)
    _user_id_var.set('')
    return rid


def current_request_id() -> str:
    return _request_id_var.get()


def set_user_id(uid: Any) -> None:
    """记录当前请求的用户标识（鉴权完成后调用）。"""
    _user_id_var.set('' if uid is None else str(uid))


def current_user_id() -> str:
    return _user_id_var.get()


def log_context() -> Dict[str, str]:
    """当前请求上下文（供 JsonFormatter 并入日志 JSON）。"""
    return {
        'request_id': current_request_id(),
        'user_id': current_user_id(),
    }


# ── 健康检查工具 ────────────────────────────────────────────────────

def measure(check_fn, label: str) -> Dict[str, Any]:
    """执行一次健康检查并测量耗时。

    Args:
        check_fn: 无参调用，返回 (ok: bool, detail: str)
        label: 检查项名称

    Returns:
        {'name', 'ok', 'latency_ms', 'detail'}
    """
    t0 = time.monotonic()
    try:
        ok, detail = check_fn()
    except Exception as e:  # 检查异常一律视为失败，绝不让 health 崩
        ok, detail = False, f'{type(e).__name__}: {e}'
    return {
        'name': label,
        'ok': bool(ok),
        'latency_ms': round((time.monotonic() - t0) * 1000, 1),
        'detail': detail,
    }


def build_health_payload(service: str, checks: List[Dict[str, Any]],
                         extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """组装健康响应。

    status=ok 仅当全部检查通过；否则 degraded（服务仍在响应，供负载均衡判活）。
    """
    all_ok = all(c.get('ok') for c in checks)
    payload: Dict[str, Any] = {
        'status': 'ok' if all_ok else 'degraded',
        'service': service,
        'checks': checks,
    }
    if extra:
        payload.update(extra)
    return payload
