#!/usr/bin/env python3
"""IM Gateway — 频道适配器注册表

提供 get_adapter(channel) 工厂，统一获取各频道适配器实例。
新增频道（如 Telegram / LINE）只需实现子类并在此注册。
"""
from .feishu import FeishuAdapter
from .wecom import WecomAdapter
from .qq import QQAdapter
from .dingtalk import DingTalkAdapter
from .telegram import TelegramAdapter
from .line import LINEAdapter

_ADAPTERS = {
    'feishu': FeishuAdapter,
    'wecom': WecomAdapter,
    'qq': QQAdapter,
    'dingtalk': DingTalkAdapter,
    'telegram': TelegramAdapter,
    'line': LINEAdapter,
}


def get_adapter(channel):
    """返回频道适配器实例，未知频道返回 None"""
    cls = _ADAPTERS.get(channel)
    return cls() if cls else None


def list_channels():
    """返回所有已注册频道标识"""
    return list(_ADAPTERS.keys())
