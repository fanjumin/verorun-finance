#!/usr/bin/env python3
"""统一网关 — OAuth Provider 注册表（Phase 2）。

新增平台 = 实现 BaseOAuthProvider + @register_oauth 注册。
"""
from .base import BaseOAuthProvider  # noqa: F401

_PROVIDERS = {}


def register_oauth(provider_cls):
    """装饰器注册 OAuth Provider"""
    _PROVIDERS[provider_cls.platform] = provider_cls
    return provider_cls


def get_oauth_provider(platform: str, config: dict = None):
    """返回平台 OAuth Provider 实例；未注册返回 None"""
    cls = _PROVIDERS.get(platform)
    if cls is None:
        return None
    return cls(config or {})


def oauth_supported(platform: str) -> bool:
    return platform in _PROVIDERS


# 导入并注册各平台实现（注册表在 import 时填充；OAUTH_PLATFORMS 须在注册后计算）
from . import twitter as _twitter  # noqa: E402,F401
from . import linkedin as _linkedin  # noqa: E402,F401
from . import reddit as _reddit  # noqa: E402,F401
from . import weibo as _weibo  # noqa: E402,F401
from . import toutiao as _toutiao  # noqa: E402,F401
from . import facebook as _facebook  # noqa: E402,F401
from . import instagram as _instagram  # noqa: E402,F401

# OAUTH_PLATFORMS：支持用户授权连接（auth_mode='oauth'）的平台
OAUTH_PLATFORMS = tuple(_PROVIDERS.keys())
