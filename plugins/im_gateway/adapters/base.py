#!/usr/bin/env python3
"""IM Gateway — 频道适配器基类

统一各即时通讯频道的接口契约，便于扩展 Telegram / LINE。
子类实现：连接测试、字段声明、消息/媒体推送。
"""
from abc import ABC, abstractmethod


class BaseIMAdapter(ABC):
    """频道适配器抽象基类"""

    #: 频道标识，如 'feishu'
    channel = ''

    #: 是否支持真实的连接测试（调用第三方 API）
    supports_test = False

    @abstractmethod
    def get_config_fields(self):
        """返回该频道的配置字段声明列表。

        每项：{'key', 'label', 'type'('text'|'password')}
        供前端渲染配置表单与后端字段白名单。
        """
        raise NotImplementedError

    @abstractmethod
    def test_connection(self, data):
        """测试频道连接。

        Args:
            data: dict，前端提交的凭据字段

        Returns:
            (ok: bool, message: str)
        """
        raise NotImplementedError

    def get_env_fallback(self):
        """返回环境变量中的频道配置（供前端参考，secret 掩码）。

        默认无环境变量兜底，子类按需覆写。
        """
        return {}

    def push_media(self, file_url, filename, mime):
        """向该频道推送媒体文件。默认不支持，子类覆写。"""
        raise Exception(f'{self.channel} does not support media push')

    # ── 工具方法 ──

    @staticmethod
    def _mask(val):
        """secret 掩码：保留前 4 位，其余用 ● 替代"""
        if val and len(val) > 4:
            return val[:4] + '●' * (len(val) - 4)
        return val
