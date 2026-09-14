#!/usr/bin/env python3
"""第三方登录提供方注册表（Phase 5，方案 A：插件自包含）。

仅负责：提供方目录、授权 URL 生成。登录闭环（回调→用户绑定→JWT）
需要 auth-center 系统集成，属方案 B，不在本模块范围。
"""
import urllib.parse


class LoginProvider:
    """登录 OAuth 提供方基类"""
    id = ''
    display_name = ''
    icon = ''
    default_scopes = ''
    authorize_endpoint = ''

    @classmethod
    def build_authorize_url(cls, client_id: str, redirect_uri: str,
                            state: str = '', scopes: str = '') -> str:
        """生成授权 URL（子类覆写参数构造）"""
        raise NotImplementedError

    @staticmethod
    def _q(params: dict) -> str:
        return urllib.parse.urlencode(params)


class WechatProvider(LoginProvider):
    id = 'wechat'
    display_name = 'WeChat'
    icon = 'W'
    default_scopes = 'snsapi_login'
    authorize_endpoint = 'https://open.weixin.qq.com/connect/qrconnect'

    @classmethod
    def build_authorize_url(cls, client_id, redirect_uri, state='', scopes=''):
        q = cls._q({
            'appid': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': scopes or cls.default_scopes,
            'state': state,
        })
        return f'{cls.authorize_endpoint}?{q}#wechat_redirect'


class QQProvider(LoginProvider):
    id = 'qq'
    display_name = 'QQ'
    icon = 'Q'
    default_scopes = 'get_user_info'
    authorize_endpoint = 'https://graph.qq.com/oauth2.0/authorize'

    @classmethod
    def build_authorize_url(cls, client_id, redirect_uri, state='', scopes=''):
        q = cls._q({
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'state': state,
            'scope': scopes or cls.default_scopes,
        })
        return f'{cls.authorize_endpoint}?{q}'


class WeiboProvider(LoginProvider):
    id = 'weibo'
    display_name = 'Weibo'
    icon = 'B'
    default_scopes = 'email'
    authorize_endpoint = 'https://api.weibo.com/oauth2/authorize'

    @classmethod
    def build_authorize_url(cls, client_id, redirect_uri, state='', scopes=''):
        q = cls._q({
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'state': state,
            'response_type': 'code',
        })
        return f'{cls.authorize_endpoint}?{q}'


class GitHubProvider(LoginProvider):
    id = 'github'
    display_name = 'GitHub'
    icon = 'GH'
    default_scopes = 'read:user user:email'
    authorize_endpoint = 'https://github.com/login/oauth/authorize'

    @classmethod
    def build_authorize_url(cls, client_id, redirect_uri, state='', scopes=''):
        q = cls._q({
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'scope': scopes or cls.default_scopes,
            'state': state,
        })
        return f'{cls.authorize_endpoint}?{q}'


class GoogleProvider(LoginProvider):
    id = 'google'
    display_name = 'Google'
    icon = 'G'
    default_scopes = 'openid email profile'
    authorize_endpoint = 'https://accounts.google.com/o/oauth2/v2/auth'

    @classmethod
    def build_authorize_url(cls, client_id, redirect_uri, state='', scopes=''):
        q = cls._q({
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': scopes or cls.default_scopes,
            'state': state,
        })
        return f'{cls.authorize_endpoint}?{q}'


# 注册表（有序，UI 渲染顺序即此）
LOGIN_PROVIDERS = [
    WechatProvider,
    QQProvider,
    WeiboProvider,
    GitHubProvider,
    GoogleProvider,
]


def list_login_providers() -> list:
    """内置提供方目录（供管理 UI 渲染卡片）"""
    return [
        {
            'id': cls.id,
            'display_name': cls.display_name,
            'icon': cls.icon,
            'default_scopes': cls.default_scopes,
            'authorize_endpoint': cls.authorize_endpoint,
        }
        for cls in LOGIN_PROVIDERS
    ]


def get_login_provider_class(provider_id: str):
    """按 id 查找提供方类，未找到返回 None"""
    for cls in LOGIN_PROVIDERS:
        if cls.id == provider_id:
            return cls
    return None
