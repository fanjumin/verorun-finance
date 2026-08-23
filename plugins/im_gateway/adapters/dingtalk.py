#!/usr/bin/env python3
"""IM Gateway — 钉钉适配器

迁移自 auth-center/routes/admin.py。使用 appkey/appsecret 获取 access_token 测试连接。
"""
from i18n import _
from .base import BaseIMAdapter


class DingTalkAdapter(BaseIMAdapter):
    channel = 'dingtalk'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'app_key', 'label': 'AppKey', 'type': 'text'},
            {'key': 'app_secret', 'label': 'AppSecret', 'type': 'password'},
            {'key': 'agent_id', 'label': 'AgentId', 'type': 'text'},
            {'key': 'corp_id', 'label': 'CorpId', 'type': 'text'},
        ]

    def test_connection(self, data):
        app_key = (data.get('app_key') or '').strip() or (data.get('appId') or '').strip()
        app_secret = (data.get('app_secret') or '').strip() or (data.get('appSecret') or '').strip()
        if not app_key or not app_secret:
            return False, _('AppKey and AppSecret cannot be empty')
        try:
            import requests as _req
            resp = _req.get(
                f'https://oapi.dingtalk.com/gettoken?appkey={app_key}&appsecret={app_secret}',
                timeout=10
            )
            rd = resp.json()
            if rd.get('access_token'):
                return True, _('DingTalk connection successful!')
            return False, f"DingTalk returned: {rd.get('errmsg', 'unknown')} (errcode={rd.get('errcode')})"
        except Exception as e:
            return False, f'Connection failed: {str(e)}'
