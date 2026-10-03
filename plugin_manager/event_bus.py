#!/usr/bin/env python3
"""
Plugin System — EventBus
==========================
Publish-subscribe event system for inter-plugin communication.

Events are identified by EventName constants.
Plugins subscribe via get_event_handlers() or bus.on().

FIN-SYS-3：异步业务事件在派发前先落盘 event_outbox，handler 失败由重发扫描
兜底（at-least-once）；DB 不可用或载荷不可 JSON 序列化时 best-effort 降级为
既有 fire-and-forget，绝不阻断业务事件。
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Callable, Any

_log = logging.getLogger('plugin_manager.event_bus')


class EventName:
    """Predefined system event constants."""
    APP_READY = 'app.ready'
    APP_SHUTDOWN = 'app.shutdown'
    USER_REGISTERED = 'user.registered'
    USER_LOGIN = 'user.login'
    USER_LOGOUT = 'user.logout'
    USER_UPDATED = 'user.updated'
    USER_DELETED = 'user.deleted'
    ORDER_CREATED = 'order.created'
    ORDER_PAID = 'order.paid'
    ORDER_REFUNDED = 'order.refunded'
    ORDER_CANCELLED = 'order.cancelled'
    ORDER_SHIPPED = 'order.shipped'
    ORDER_COMPLETED = 'order.completed'
    SUB_CREATED = 'sub.created'
    SUB_RENEWED = 'sub.renewed'
    SUB_EXPIRED = 'sub.expired'
    SUB_CANCELLED = 'sub.cancelled'
    CMS_CONTENT_PUBLISHED = 'cms.published'
    CMS_CONTENT_UPDATED = 'cms.updated'
    CMS_CONTENT_DELETED = 'cms.deleted'
    SCHEDULER_JOB_STARTED = 'scheduler.job_started'
    SCHEDULER_JOB_COMPLETED = 'scheduler.job_completed'
    SCHEDULER_JOB_FAILED = 'scheduler.job_failed'
    HEALTH_CHECK_PASSED = 'health.passed'
    HEALTH_CHECK_WARNING = 'health.warning'
    HEALTH_CHECK_ERROR = 'health.error'
    PLUGIN_INSTALLED = 'plugin.installed'
    PLUGIN_ENABLED = 'plugin.enabled'
    PLUGIN_DISABLED = 'plugin.disabled'
    PLUGIN_UNINSTALLED = 'plugin.uninstalled'
    # Kernel patch B: emitted after each agent run completes (success or failure)
    AGENT_TASK_COMPLETED = 'agent.task.completed'


class EventBus:
    """Simple in-process publish-subscribe event bus.

    生命周期类事件（plugin.*/app.*/scheduler.*/health.*）默认同步执行，
    保证插件启停、应用初始化的时序正确；其余业务事件（order.*/user.* 等）
    默认丢入线程池异步执行，避免慢 handler（发通知/邮件）阻塞主请求。
    """

    # 需要保证时序、必须同步执行的事件名前缀
    _SYNC_PREFIXES = ('plugin.', 'app.', 'scheduler.', 'health.')

    def __init__(self, max_workers: int = 4):
        self._handlers: Dict[str, list] = {}
        self._lock = threading.Lock()
        self._max_workers = max_workers
        self._executor = None
        self._exec_lock = threading.Lock()
        # PF-09：handler 失败计数 {event: n}，供健康检查/管理页观测，
        # 不改变 outbox 重发语义（失败仍返回错误描述由调用方回写）。
        self._fail_counts: Dict[str, int] = {}

    def _get_executor(self) -> ThreadPoolExecutor:
        """懒加载线程池（daemon 线程，进程退出不阻塞）"""
        if self._executor is None:
            with self._exec_lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self._max_workers,
                        thread_name_prefix='eventbus',
                    )
        return self._executor

    def on(self, event: str, handler: Callable):
        """Subscribe to an event."""
        with self._lock:
            self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable = None):
        """Unsubscribe. If handler is None, removes all handlers for event."""
        with self._lock:
            if handler is None:
                self._handlers.pop(event, None)
            else:
                handlers = self._handlers.get(event, [])
                self._handlers[event] = [h for h in handlers if h is not handler]

    def _record_handler_failure(self, event: str, reason: str) -> None:
        """PF-09：累加事件失败计数并走 logging 通道（禁止 print 噪声）。"""
        with self._lock:
            n = self._fail_counts.get(event, 0) + 1
            self._fail_counts[event] = n
        _log.warning('[EventBus] handler failure #%d for %s: %s', n, event, reason)

    def handler_failure_counts(self) -> Dict[str, int]:
        """返回各事件 handler 累计失败次数的快照（PF-09 观测口）。"""
        with self._lock:
            return dict(self._fail_counts)

    def reset_handler_failures(self) -> None:
        """清零失败计数（测试或人工处置后使用）。"""
        with self._lock:
            self._fail_counts.clear()

    def _run_handler(self, event: str, handler: Callable, kwargs: dict):
        """执行单个 handler，捕获异常避免线程池吞错（含 SystemExit，P0-4）。

        成功返回 True；失败返回错误描述字符串（FIN-SYS-3 据此回写 outbox）。
        """
        try:
            handler(**kwargs)
            return True
        except SystemExit as e:
            reason = f'SystemExit: {e}'
            self._record_handler_failure(event, reason)
            return reason
        except Exception as e:
            reason = f'{e.__class__.__name__}: {e}'
            self._record_handler_failure(event, reason)
            return reason

    def _handlers_for(self, event: str) -> list:
        with self._lock:
            return list(self._handlers.get(event, []))

    def _dispatch_async(self, event: str, row_id, kwargs: dict):
        """线程池任务：顺序执行全部 handler，最后回写 outbox 终态。

        row_id 为 None 表示本次未落盘（降级路径），只派发不回写。
        """
        err = ''
        for handler in self._handlers_for(event):
            outcome = self._run_handler(event, handler, kwargs)
            if outcome is not True:
                err = outcome or 'handler failed'
        _outbox_finish(row_id, err == '', err)

    def emit(self, event: str, sync: bool = None, **kwargs):
        """Emit an event with keyword arguments.

        Args:
            event: 事件名
            sync: 是否同步执行。None(默认) 时按事件名前缀自动判定——
                  生命周期事件同步、业务事件异步；显式传 True/False 可覆盖。
        """
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        if not handlers:
            return

        if sync is None:
            sync = event.startswith(self._SYNC_PREFIXES)

        if sync:
            for handler in handlers:
                self._run_handler(event, handler, kwargs)
        else:
            # FIN-SYS-3：业务事件先落盘 event_outbox 再派发，handler 失败由
            # 重发扫描兜底（at-least-once）。落盘不可用/载荷不可序列化时
            # row_id 为 None，退化为既有 fire-and-forget，绝不阻断业务事件。
            row_id = _outbox_insert(event, kwargs)
            self._get_executor().submit(self._dispatch_async, event, row_id, kwargs)

    def clear(self):
        """Remove all handlers (for testing)."""
        with self._lock:
            self._handlers.clear()
            self._fail_counts.clear()

    def shutdown(self, wait: bool = False):
        """优雅关闭线程池（供 app.shutdown 调用，可选）。"""
        with self._exec_lock:
            if self._executor is not None:
                self._executor.shutdown(wait=wait)
                self._executor = None


# Module-level singleton
_BUS = None
_BUS_LOCK = threading.Lock()


def get_event_bus() -> EventBus:
    """Get the global EventBus singleton."""
    global _BUS
    if _BUS is None:
        with _BUS_LOCK:
            if _BUS is None:
                _BUS = EventBus()
    return _BUS


# ══════════════════════════════════════════════════════════════════════════
# FIN-SYS-3 · 事件落盘待发层（outbox）
#
# 业务事件（异步）派发前先写入 event_outbox；handler 失败按指数退避重排，
# 超过上限转 dead —— 告警等关键投递由 at-most-once 升为 at-least-once。
# DB 不可用 / 载荷不可 JSON 序列化时 best-effort 降级，不改既有语义。
# ══════════════════════════════════════════════════════════════════════════

EVENT_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS event_outbox (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event         TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','done','dead')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_until  TIMESTAMPTZ,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_due
    ON event_outbox(status, next_retry_at);
"""

# 与 plugin_registry DDL 不同的锁号，避免初始化时互相阻塞
_OUTBOX_DDL_LOCK_KEY = 775221

_MAX_ATTEMPTS = int(os.environ.get('EVENT_OUTBOX_MAX_ATTEMPTS', 5))
_LEASE_SECONDS = int(os.environ.get('EVENT_OUTBOX_LEASE_SECONDS', 60))
_BACKOFF_CAP = int(os.environ.get('EVENT_OUTBOX_BACKOFF_CAP', 300))
_SCAN_LIMIT = 50

_resend_thread = None
_resend_lock = threading.Lock()


def _outbox_enabled() -> bool:
    return os.environ.get('EVENT_OUTBOX_ENABLED', '1') not in ('0', 'false', 'False')


def _get_conn():
    """主库连接（与 plugin_registry 同库）。延迟导入避免循环依赖。"""
    from .models import get_registry_db
    return get_registry_db()


def _jsonable(kwargs: dict):
    """可 JSON 序列化则返回字符串，否则 None（调用方走降级路径）。"""
    try:
        return json.dumps(kwargs, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _outbox_insert(event: str, kwargs: dict):
    """落盘一条待发事件；不可序列化或 DB 异常 → None（降级 fire-and-forget）。"""
    if not _outbox_enabled():
        return None
    payload = _jsonable(kwargs)
    if payload is None:
        _log.debug('[EventBus] payload not serializable, skip outbox: %s', event)
        return None
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "INSERT INTO event_outbox (event, payload) VALUES (?, ?) "
                "RETURNING id", (event, payload)).fetchone()
            conn.commit()
        return row['id'] if row else None
    except Exception as e:
        _log.warning('[EventBus] outbox insert failed, degrade to fire-and-forget: %s', e)
        return None


def _outbox_finish(row_id, ok: bool, err: str = ''):
    """回写结果：成功置 done；失败累加 attempts 并按退避重排（超限转 dead）。"""
    if row_id is None or not _outbox_enabled():
        return
    try:
        with _get_conn() as conn:
            if ok:
                conn.execute(
                    "UPDATE event_outbox SET status = 'done', last_error = '', "
                    "updated_at = NOW() WHERE id = ?", (row_id,))
            else:
                conn.execute(
                    "UPDATE event_outbox SET "
                    "attempts = attempts + 1, "
                    "last_error = ?, "
                    "status = CASE WHEN attempts + 1 >= ? THEN 'dead' ELSE 'pending' END, "
                    "next_retry_at = NOW() + "
                    "  LEAST(?, 5 * POWER(2, attempts)) * INTERVAL '1 second', "
                    "locked_until = NULL, updated_at = NOW() "
                    "WHERE id = ?",
                    (str(err)[:500], _MAX_ATTEMPTS, _BACKOFF_CAP, row_id))
            conn.commit()
    except Exception as e:
        _log.warning('[EventBus] outbox finish failed id=%s: %s', row_id, e)


def resend_pending() -> int:
    """重发到期的 pending 事件（租约抢占，多 worker 安全）。返回派发条数。"""
    if not _outbox_enabled():
        return 0
    bus = get_event_bus()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "UPDATE event_outbox SET "
                "locked_until = NOW() + ? * INTERVAL '1 second', updated_at = NOW() "
                "WHERE id IN ("
                "  SELECT id FROM event_outbox "
                "   WHERE status = 'pending' AND next_retry_at <= NOW() "
                "     AND (locked_until IS NULL OR locked_until < NOW()) "
                "   ORDER BY next_retry_at LIMIT ?"
                ") RETURNING id, event, payload",
                (_LEASE_SECONDS, _SCAN_LIMIT)).fetchall()
            conn.commit()
    except Exception as e:
        _log.warning('[EventBus] outbox resend scan failed: %s', e)
        return 0

    executor = bus._get_executor()
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        executor.submit(bus._dispatch_async, row['event'], row['id'], payload)
    return len(rows)


def _resend_loop(interval: float):
    while True:
        time.sleep(interval)
        try:
            resend_pending()
        except Exception as e:      # 兜底：扫描异常绝不终止守护线程
            _log.warning('[EventBus] resend loop error: %s', e)


def start_resend_worker(interval: float = None):
    """懒启动重发扫描守护线程（进程内单例）。"""
    if not _outbox_enabled():
        return
    global _resend_thread
    with _resend_lock:
        if _resend_thread is not None and _resend_thread.is_alive():
            return
        interval = interval or float(os.environ.get('EVENT_OUTBOX_RETRY_INTERVAL', 30))
        _resend_thread = threading.Thread(
            target=_resend_loop, args=(interval,),
            name='event-outbox-resend', daemon=True)
        _resend_thread.start()


def init_event_outbox_table():
    """初始化 event_outbox 表并启动重发线程（幂等；advisory lock 串行化 DDL）。"""
    with _get_conn() as conn:
        conn.execute('SELECT pg_advisory_lock(%s)', (_OUTBOX_DDL_LOCK_KEY,))
        try:
            conn.executescript(EVENT_OUTBOX_DDL)
        finally:
            try:
                conn.execute('SELECT pg_advisory_unlock(%s)', (_OUTBOX_DDL_LOCK_KEY,))
                conn.commit()
            except Exception:
                pass   # 事务已中止/连接将关闭时，锁由会话结束自动释放
    start_resend_worker()
