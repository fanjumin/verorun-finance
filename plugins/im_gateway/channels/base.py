#!/usr/bin/env python3
"""BaseChannelAdapter — 统一渠道适配器抽象基类（增量新增，Phase 0）。

与现有 BaseIMAdapter 并存：现有 IM 适配器保持不动，
新渠道（社媒/小程序/公众号）继承本类并注册到统一注册表。
"""
from abc import ABC, abstractmethod


class BaseChannelAdapter(ABC):
    """统一渠道适配器抽象基类

    channel_type: 'im' | 'social' | 'mini_app'
    auth_mode:    'oauth' | 'client_credential' | 'manual'
    """

    channel = ''            # 唯一渠道标识，如 'twitter' / 'telegram'
    channel_type = ''       # 'im' | 'social' | 'mini_app'
    auth_mode = 'manual'
    supports_test = False

    @abstractmethod
    def get_config_fields(self):
        """配置字段声明：[{'key','label','type':'text'|'password'}]"""
        raise NotImplementedError

    @abstractmethod
    def test_connection(self, data):
        """连接测试，返回 (ok: bool, message: str)"""
        raise NotImplementedError

    # ── 投递（IM 消息与社媒发布的统一入口）──
    def send(self, payload: dict, **kw) -> dict:
        """向渠道投递内容。返回 {'success': bool, 'error'?: str, ...}"""
        raise NotImplementedError

    # ── 状态跟踪（可选）──
    def get_status(self, task_id: str):
        return None

    # ── OAuth 授权（auth_mode == 'oauth' 时实现）──
    def get_authorize_url(self, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        raise NotImplementedError

    def refresh_token(self, refresh_token: str) -> dict:
        raise NotImplementedError
