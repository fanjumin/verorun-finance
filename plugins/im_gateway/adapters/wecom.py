#!/usr/bin/env python3
"""IM Gateway — 企业微信适配器

迁移自 auth-center/routes/admin.py。修复 channel_name → channel 列名 bug。
"""
from i18n import _
import os
import json as _json
import base64
import urllib.request as _ur

from .base import BaseIMAdapter


class WecomAdapter(BaseIMAdapter):
    channel = 'wecom'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'corp_id', 'label': 'Enterprise ID', 'type': 'text'},
            {'key': 'agent_id', 'label': 'AgentId', 'type': 'text'},
            {'key': 'secret', 'label': 'Secret', 'type': 'password'},
            {'key': 'touser', 'label': 'Default Recipient', 'type': 'text'},
            {'key': 'token', 'label': 'Callback Token', 'type': 'password'},
            {'key': 'encoding_aes_key', 'label': 'EncodingAESKey', 'type': 'password'},
        ]

    def test_connection(self, data):
        corp_id = (data.get('corp_id') or '').strip()
        secret = (data.get('secret') or '').strip()
        if not corp_id or not secret:
            return False, _('Enterprise ID and Secret cannot be empty')
        try:
            import requests as _req
            resp = _req.get(
                f'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corp_id}&corpsecret={secret}',
                timeout=10
            )
            rd = resp.json()
            if rd.get('access_token'):
                return True, _('WeCom connection successful!')
            return False, f"WeCom returned: {rd.get('errmsg', 'unknown')} (errcode={rd.get('errcode')})"
        except Exception as e:
            return False, f'Connection failed: {str(e)}'

    def get_env_fallback(self):
        cfg = {}
        corp_id = os.environ.get('WECOM_CORP_ID', '')
        secret = os.environ.get('WECOM_SECRET', '')
        agent_id = os.environ.get('WECOM_AGENT_ID', '')
        touser = os.environ.get('WECOM_TOUSER', '')
        token = os.environ.get('WECOM_TOKEN', '')
        aes_key = os.environ.get('WECOM_ENCODING_AES_KEY', '')
        if corp_id:
            cfg['corp_id'] = corp_id
        if secret:
            cfg['secret'] = self._mask(secret)
        if agent_id:
            cfg['agent_id'] = agent_id
        if touser:
            cfg['touser'] = touser
        if token:
            cfg['token'] = self._mask(token)
        if aes_key:
            cfg['encoding_aes_key'] = self._mask(aes_key)
        return cfg

    # ── 媒体推送 ──

    def push_media(self, file_url, filename, mime):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='wecom' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("WeCom channel is not configured"))
        cfg = _json.loads(row['config_json'])
        webhook = cfg.get('webhook_url', '')
        if not webhook:
            raise Exception(_("WeCom webhook_url is empty"))
        if mime.startswith('image/'):
            body = {"msgtype": "image", "image": {"base64": self._fetch_as_base64(file_url), "md5": ""}}
        elif mime.startswith('video/') or mime.startswith('audio/'):
            body = {"msgtype": "file", "file": {"media_id": _("File upload not supported")}}
        else:
            body = {"msgtype": "markdown",
                    "markdown": {"content": _("**{}**\n[下载文件]({})").format(filename, file_url)}}
        resp = _json.loads(_ur.urlopen(_ur.Request(webhook,
            data=_json.dumps(body).encode(), headers={'Content-Type': 'application/json'}
        )).read())
        if resp.get('errcode', -1) != 0:
            raise Exception(resp.get('errmsg', _('WeCom push failed')))

    @staticmethod
    def _fetch_as_base64(url):
        data = _ur.urlopen(url).read()
        return base64.b64encode(data).decode()
