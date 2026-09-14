#!/usr/bin/env python3
"""第三方登录提供方管理子模块（Phase 5，方案 A：插件自包含）。"""
from .providers import (  # noqa: F401
    list_login_providers,
    get_login_provider_class,
)
