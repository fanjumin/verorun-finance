#!/usr/bin/env python3
"""
Plugin Manager — 钩子植入点（系统关键路径 Hook 注入）
==========================================================
提供便捷函数，在系统关键路径植入 HookRegistry action 调用，
使插件可以监听系统事件。

用法:
    from plugin_manager.injectors import fire_hook
    fire_hook('user/registered', user_id=user.id)
"""

from typing import Any, Dict


def fire_hook(hook_name: str, **kwargs):
    """触发指定钩子（从当前 Flask app 获取 HookRegistry 实例）

    可安全重复调用：如果 PluginManager 未初始化，静默跳过。
    """
    try:
        from flask import current_app
        mgr = current_app.extensions.get('plugin_manager')
        if mgr and mgr._hook_registry:
            mgr._hook_registry.do_action(hook_name, **kwargs)
    except (RuntimeError, KeyError, AttributeError):
        pass  # 不在 Flask 请求上下文、或 PluginManager 未初始化时静默跳过


def fire_filter(hook_name: str, value, **kwargs):
    """触发指定 filter 钩子"""
    try:
        from flask import current_app
        mgr = current_app.extensions.get('plugin_manager')
        if mgr and mgr._hook_registry:
            return mgr._hook_registry.apply_filters(hook_name, value, **kwargs)
    except (RuntimeError, KeyError, AttributeError):
        pass
    return value
