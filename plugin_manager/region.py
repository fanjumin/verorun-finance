#!/usr/bin/env python3
"""
Region Router — 统一区域路由模块
===================================
根据环境变量 APP_REGION 动态返回正确的 API 端点。
所有需要连接远程服务的模块统一通过此模块获取 URL。

环境变量:
  APP_REGION    cn | global (默认 global)
  API_BASE_CN       境内 API 基础 URL (默认 https://api.verorun.cn/v1)
  API_BASE_GLOBAL   境外 API 基础 URL (默认 https://api.verorun.com/v1)
  GUARDIAN_URL_CN   境内 VeroGuard 端点 (默认 https://api.verorun.cn)
  GUARDIAN_URL_GLOBAL 境外 VeroGuard 端点 (默认 https://api.verorun.com)
"""
import os
import threading
from typing import Optional

_REGION_CACHE: Optional[str] = None
_REGION_LOCK = threading.Lock()


def get_region() -> str:
    """获取当前部署区域标识。

    Returns:
        'cn' 或 'global'
    """
    global _REGION_CACHE
    if _REGION_CACHE is not None:
        return _REGION_CACHE
    with _REGION_LOCK:
        if _REGION_CACHE is not None:
            return _REGION_CACHE
        region = os.environ.get('APP_REGION', 'global').strip().lower()
        if region not in ('cn', 'global'):
            region = 'global'
        _REGION_CACHE = region
        return _REGION_CACHE


def get_api_base() -> str:
    """获取通用 API 基础 URL（License、Store、订阅等）。

    Returns:
        如 'https://api.verorun.cn/v1' 或 'https://api.verorun.com/v1'
    """
    region = get_region()
    if region == 'cn':
        return os.environ.get('API_BASE_CN', 'https://api.verorun.cn/v1')
    return os.environ.get('API_BASE_GLOBAL', 'https://api.verorun.com/v1')


def get_veroguard_url() -> str:
    """获取 VeroGuard 心跳上报端点 URL。

    Returns:
        如 'https://api.verorun.cn' 或 'https://api.verorun.com'
    """
    region = get_region()
    if region == 'cn':
        return os.environ.get('GUARDIAN_URL_CN', 'https://api.verorun.cn')
    return os.environ.get('GUARDIAN_URL_GLOBAL', 'https://api.verorun.com')


def get_license_service_url() -> str:
    """获取 LicenseService 心跳 URL。

    Returns:
        如 'https://api.verorun.cn/api/subscription/heartbeat'
    """
    return f"{get_api_base().rstrip('/v1')}/api/subscription/heartbeat"


def is_cn_region() -> bool:
    """便捷方法：是否为境内区域"""
    return get_region() == 'cn'


def reset_region_cache():
    """重置区域缓存（仅用于测试）"""
    global _REGION_CACHE
    with _REGION_LOCK:
        _REGION_CACHE = None
