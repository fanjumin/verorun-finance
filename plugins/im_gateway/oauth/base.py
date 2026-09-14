#!/usr/bin/env python3
"""BaseOAuthProvider — OAuth 授权码流抽象基类（Phase 2，对标 Postiz SocialProvider）。

每个平台实现 get_authorize_url / exchange_code；支持 refresh_token 的平台实现刷新。
config 为应用凭据字典（client_id / client_secret 等），键名与 social_push 全局配置一致。
"""
from abc import ABC, abstractmethod


class BaseOAuthProvider(ABC):
    platform = ''
    SCOPE = ''
    AUTHORIZE_URL = ''
    TOKEN_URL = ''
    # 是否支持 refresh_token 刷新（facebook/instagram/twitter/weibo 为 False）
    supports_refresh = False

    def __init__(self, config: dict = None):
        self.config = config or {}

    @abstractmethod
    def get_authorize_url(self, state: str, redirect_uri: str):
        """构造授权 URL。

        返回 str；若需把额外状态（如 OAuth 1.0a token_secret）随回调带回，
        返回 (url, {extra_state})，由路由合并进 state 参数。
        """
        raise NotImplementedError

    @abstractmethod
    def exchange_code(self, code: str, redirect_uri: str, **extras) -> dict:
        """code 换 token。返回 {'access_token','refresh_token','expires_in','uid','handle'}"""
        raise NotImplementedError

    def refresh_token(self, refresh_token: str) -> dict:
        """刷新 token（无刷新机制的平台抛 NotImplementedError）"""
        raise NotImplementedError
