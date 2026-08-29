#!/usr/bin/env python3
"""
Plugin Manager — 运行时故障隔离守卫（P0-4）
=============================================
统一插件调用边界 + 熔断计数，防止第三方插件崩溃拖垮宿主 worker：

  - record_failure() / reset_failures() / should_trip()：每插件连续失败熔断计数
  - safe_call()：统一调用边界（捕获 Exception/SystemExit，不向外传播）
  - 官方插件（OFFICIAL_PLUGIN_IDS）自动豁免，不计入熔断

熔断触发后由 PluginManager._maybe_trip() 执行自动禁用（本模块不依赖 manager，
避免循环导入）。
"""

import os
import threading
from typing import Dict, Callable, Any, Optional

from .watermark import OFFICIAL_PLUGIN_IDS

# 熔断阈值：连续失败次数（环境变量可配，默认 3）
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get('PLUGIN_CIRCUIT_BREAKER_THRESHOLD', '3'))

_lock = threading.Lock()
_failures: Dict[str, int] = {}


def _exempt(plugin_id: str) -> bool:
    """官方插件豁免熔断。"""
    return plugin_id in OFFICIAL_PLUGIN_IDS


def record_failure(plugin_id: str) -> int:
    """记录插件一次失败（官方豁免），返回当前连续失败次数。"""
    if _exempt(plugin_id):
        return 0
    with _lock:
        n = _failures.get(plugin_id, 0) + 1
        _failures[plugin_id] = n
        return n


def reset_failures(plugin_id: str):
    """插件成功执行后复位熔断计数。"""
    if _exempt(plugin_id):
        return
    with _lock:
        _failures.pop(plugin_id, None)


def failure_count(plugin_id: str) -> int:
    """当前连续失败次数。"""
    with _lock:
        return _failures.get(plugin_id, 0)


def should_trip(plugin_id: str) -> bool:
    """连续失败次数达到阈值 → 应触发熔断。"""
    return failure_count(plugin_id) >= CIRCUIT_BREAKER_THRESHOLD


def safe_call(plugin_id: str, fn: Callable, *args, _context: str = '',
              **kwargs) -> Optional[Any]:
    """统一插件调用边界（P0-4）。

    捕获插件抛出的 Exception / SystemExit，记录失败并返回 None；
    成功则复位熔断计数。异常不向外传播，避免拖垮宿主 worker。
    """
    tag = f' | {_context}' if _context else ''
    try:
        result = fn(*args, **kwargs)
        reset_failures(plugin_id)
        return result
    except SystemExit as e:
        record_failure(plugin_id)
        print(f'[Guard] ⚠️ {plugin_id}: SystemExit({e}) intercepted{tag}')
        return None
    except Exception as e:
        record_failure(plugin_id)
        print(f'[Guard] ⚠️ {plugin_id}: {type(e).__name__}: {e}{tag}')
        return None
