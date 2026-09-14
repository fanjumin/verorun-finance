#!/usr/bin/env python3
"""统一渠道注册表 — 新增渠道 = 实现 BaseChannelAdapter + 注册（Phase 0）。

现有 IM 适配器保持不动；本注册表只登记新增的社媒/小程序等渠道。
"""
from .base import BaseChannelAdapter  # noqa: F401

_REGISTRY: dict = {}


def register_channel_adapter(adapter_cls):
    """装饰器注册：@register_channel_adapter 到渠道实现类上"""
    instance = adapter_cls()
    _REGISTRY[instance.channel] = instance
    return adapter_cls


def get_channel_adapter(channel: str):
    """返回渠道适配器实例，未注册返回 None"""
    return _REGISTRY.get(channel)


def list_channels() -> list:
    """返回所有已注册新增渠道的元信息列表"""
    return [
        {
            'channel': a.channel,
            'channel_type': a.channel_type,
            'auth_mode': a.auth_mode,
            'supports_test': a.supports_test,
        }
        for a in _REGISTRY.values()
    ]


# 导入社媒渠道子包（装饰器在 import 时完成注册；须在 _REGISTRY 定义之后）
from . import social  # noqa: E402,F401
