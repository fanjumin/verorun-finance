#!/usr/bin/env python3
"""
VeroGuard — 运行时环境检测模块（Phase 3）
=============================================
检测调试器、可疑模块、环境变量变更。
"""
import os
import time as _time
import logging

# 守护进程启动时间
START_TIME = _time.time()


def _check_ptrace() -> bool:
    """检测是否有调试器附加（Linux /proc/self/status TracerPid）"""
    try:
        with open('/proc/self/status', 'r') as f:
            for line in f:
                if line.startswith('TracerPid:'):
                    return line.split(':')[1].strip() != '0'
    except Exception:
        pass
    return False


def _check_loaded_modules() -> list:
    """检查是否有可疑的 Python 模块被加载"""
    suspicious = []
    try:
        import sys
        for mod_name in sorted(sys.modules.keys()):
            if any(kw in mod_name for kw in ('frida', 'pdb', 'pydevd',
                                               'debugpy', 'mock')):
                if mod_name not in suspicious:
                    suspicious.append(mod_name)
    except Exception:
        pass
    return suspicious


def _check_env_vars() -> dict:
    """检查关键环境变量是否被篡改"""
    changed = {}
    # 检查可能被调试器设置的环境变量
    for var in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH',
                'PYTHONSTARTUP', 'PYTHONINSPECT'):
        val = os.environ.get(var, '')
        if val:
            changed[var] = val
    return changed


def check() -> dict:
    """
    运行所有运行时检测，返回结果。

    返回值:
        {
            "debugger_detected": bool,
            "suspicious_modules": list,
            "modified_env_vars": dict,
            "uptime_seconds": int,
        }
    """
    result = {
        "debugger_detected": _check_ptrace(),
        "suspicious_modules": _check_loaded_modules(),
        "modified_env_vars": _check_env_vars(),
        "uptime_seconds": int(_time.time() - START_TIME),
    }

    if result["debugger_detected"]:
        logging.warning("Runtime: debugger detected!")
    if result["suspicious_modules"]:
        logging.warning("Runtime: suspicious modules: %s",
                       result["suspicious_modules"])

    return result
