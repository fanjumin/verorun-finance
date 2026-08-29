#!/usr/bin/env python3
"""
Plugin Manager — HookRegistry (Action + Filter 双钩子系统)
==============================================================
借鉴 WordPress 的钩子机制:
  - Action: 执行一个操作（不返回值）
  - Filter: 修改数据（返回值）

用法:
    hooks = HookRegistry()

    # 注册 Action
    hooks.add_action('user.registered', send_welcome_email)
    hooks.do_action('user.registered', user_id=123)

    # 注册 Filter
    hooks.add_filter('content.render', highlight_keywords)
    result = hooks.apply_filters('content.render', html_content)

优先级: 数字越小越先执行（默认 10）。
"""

import threading
import inspect
from typing import Dict, List, Callable, Any, Optional


class _Hook:
    """单个钩子点的内部数据结构"""
    __slots__ = ('callback', 'priority', 'identifier')

    def __init__(self, callback: Callable, priority: int, identifier: str = ''):
        self.callback = callback
        self.priority = priority
        self.identifier = identifier


class HookRegistry:
    """Action + Filter 双钩子系统"""

    def __init__(self):
        self._actions: Dict[str, List[_Hook]] = {}
        self._filters: Dict[str, List[_Hook]] = {}
        self._lock = threading.RLock()

    # ── Action ────────────────────────────────────────────────────────

    def add_action(self, hook: str, callback: Callable, priority: int = 10,
                   identifier: str = ''):
        """注册一个 action 钩子

        Args:
            hook: 钩子名称（如 'user.registered'）
            callback: 回调函数
            priority: 优先级（越小越先执行）
            identifier: 来源标识（如插件名）
        """
        with self._lock:
            self._actions.setdefault(hook, [])
            self._actions[hook].append(_Hook(callback, priority, identifier))
            self._actions[hook].sort(key=lambda h: h.priority)

    def remove_action(self, hook: str, callback: Callable = None,
                      identifier: str = ''):
        """移除 action 钩子"""
        with self._lock:
            if hook not in self._actions:
                return
            if callback is None and not identifier:
                del self._actions[hook]
                return
            self._actions[hook] = [
                h for h in self._actions[hook]
                if (callback is not None and h.callback is callback) is False
                and (identifier and h.identifier == identifier) is False
            ]
            if not self._actions[hook]:
                del self._actions[hook]

    def do_action(self, hook: str, *args, **kwargs):
        """执行 action 钩子

        同步调用所有已注册的回调，不返回值。
        异常会被捕获并打印，不会传播。
        """
        with self._lock:
            hooks = list(self._actions.get(hook, []))

        for h in hooks:
            try:
                h.callback(*args, **kwargs)
            except SystemExit as e:
                _hook_failure(h, hook, e)
            except Exception as e:
                _hook_failure(h, hook, e)

    def has_action(self, hook: str, callback: Callable = None) -> bool:
        """检查是否注册了指定 action"""
        with self._lock:
            hooks = self._actions.get(hook, [])
            if callback is None:
                return len(hooks) > 0
            return any(h.callback is callback for h in hooks)

    # ── Filter ───────────────────────────────────────────────────────

    def add_filter(self, hook: str, callback: Callable, priority: int = 10,
                   identifier: str = ''):
        """注册一个 filter 钩子

        Args:
            hook: 过滤器名称（如 'content.render'）
            callback: 回调函数，接收 (value, **kwargs) 返回修改后的值
            priority: 优先级
            identifier: 来源标识
        """
        with self._lock:
            self._filters.setdefault(hook, [])
            # 幂等去重：同一 (hook, identifier) 不重复注册
            # 修复 setup()->on_enable() 与 register_routes() 双注册导致过滤器被执行两次
            if identifier and any(h.identifier == identifier for h in self._filters[hook]):
                return
            self._filters[hook].append(_Hook(callback, priority, identifier))
            self._filters[hook].sort(key=lambda h: h.priority)

    def remove_filter(self, hook: str, callback: Callable = None,
                      identifier: str = ''):
        """移除 filter 钩子"""
        with self._lock:
            if hook not in self._filters:
                return
            if callback is None and not identifier:
                del self._filters[hook]
                return
            self._filters[hook] = [
                h for h in self._filters[hook]
                if (callback is not None and h.callback is callback) is False
                and (identifier and h.identifier == identifier) is False
            ]
            if not self._filters[hook]:
                del self._filters[hook]

    def apply_filters(self, hook: str, value, **kwargs):
        """执行 filter 钩子链

        将 value 依次传入每个已注册的回调，
        每个回调的返回值作为下一个回调的输入。
        """
        with self._lock:
            hooks = list(self._filters.get(hook, []))

        for h in hooks:
            try:
                # Filter 回调签名: callback(value, **kwargs) -> new_value
                sig = inspect.signature(h.callback)
                if 'value' in sig.parameters or len(sig.parameters) == 0:
                    value = h.callback(value, **kwargs)
                else:
                    value = h.callback(value)
            except Exception as e:
                print(f'[HookRegistry] filter "{hook}" error '
                      f'(from {h.identifier}): {e}')
                import traceback
                traceback.print_exc()
        return value

    def has_filter(self, hook: str, callback: Callable = None) -> bool:
        """检查是否注册了指定 filter"""
        with self._lock:
            hooks = self._filters.get(hook, [])
            if callback is None:
                return len(hooks) > 0
            return any(h.callback is callback for h in hooks)

    # ── 批量操作 ────────────────────────────────────────────────────

    def remove_all(self, identifier: str = ''):
        """移除指定来源的所有钩子（插件禁用/卸载时调用）"""
        with self._lock:
            if identifier:
                for hook in list(self._actions.keys()):
                    self._actions[hook] = [
                        h for h in self._actions[hook]
                        if h.identifier != identifier
                    ]
                    if not self._actions[hook]:
                        del self._actions[hook]
                for hook in list(self._filters.keys()):
                    self._filters[hook] = [
                        h for h in self._filters[hook]
                        if h.identifier != identifier
                    ]
                    if not self._filters[hook]:
                        del self._filters[hook]
            else:
                self._actions.clear()
                self._filters.clear()

    def list_actions(self, hook: str = None) -> Dict[str, List[dict]]:
        """列出所有 action 钩子"""
        with self._lock:
            if hook:
                return {hook: [
                    {'identifier': h.identifier, 'priority': h.priority}
                    for h in self._actions.get(hook, [])
                ]}
            result = {}
            for k, v in self._actions.items():
                result[k] = [
                    {'identifier': h.identifier, 'priority': h.priority}
                    for h in v
                ]
            return result

    def list_filters(self, hook: str = None) -> Dict[str, List[dict]]:
        """列出所有 filter 钩子"""
        with self._lock:
            if hook:
                return {hook: [
                    {'identifier': h.identifier, 'priority': h.priority}
                    for h in self._filters.get(hook, [])
                ]}
            result = {}
            for k, v in self._filters.items():
                result[k] = [
                    {'identifier': h.identifier, 'priority': h.priority}
                    for h in v
                ]
            return result


def _hook_failure(h: _Hook, hook: str, error: BaseException):
    """钩子回调失败统一处理（P0-4）：有插件来源时计入熔断计数 + 日志。"""
    if h.identifier:
        from .guard import record_failure
        record_failure(h.identifier)
    print(f'[HookRegistry] {type(error).__name__} "{hook}" '
          f'(from {h.identifier or "system"}): {error}')
    import traceback
    traceback.print_exc()


# ── 模块级单例 ──────────────────────────────────────────────────────

_HOOKS = None
_HOOKS_LOCK = threading.Lock()


def get_hook_registry() -> HookRegistry:
    """获取全局 HookRegistry 单例"""
    global _HOOKS
    if _HOOKS is None:
        with _HOOKS_LOCK:
            if _HOOKS is None:
                _HOOKS = HookRegistry()
    return _HOOKS
