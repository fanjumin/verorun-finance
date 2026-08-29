#!/usr/bin/env python3
"""
Plugin System — EventBus
==========================
Publish-subscribe event system for inter-plugin communication.

Events are identified by EventName constants.
Plugins subscribe via get_event_handlers() or bus.on().
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Callable, Any


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

    def _run_handler(self, event: str, handler: Callable, kwargs: dict):
        """执行单个 handler，捕获异常避免线程池吞错（含 SystemExit，P0-4）。"""
        try:
            handler(**kwargs)
        except SystemExit as e:
            print(f'[EventBus] handler SystemExit for {event}: {e}')
        except Exception as e:
            print(f'[EventBus] handler error for {event}: {e}')

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
            executor = self._get_executor()
            for handler in handlers:
                executor.submit(self._run_handler, event, handler, kwargs)

    def clear(self):
        """Remove all handlers (for testing)."""
        with self._lock:
            self._handlers.clear()

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
