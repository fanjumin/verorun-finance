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
            return False, _('WeCom returned: {} (errcode={})').format(rd.get('errmsg', 'unknown'), rd.get('errcode'))
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

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

    # ── 消息发送 ──

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='wecom' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("WeCom channel is not configured"))
        return _json.loads(row['config_json'])

    def send(self, payload: dict, **kw) -> dict:
        """发送文本消息：优先群机器人 webhook_url；无则回退企业应用 message/send。"""
        content = payload.get('content') or payload.get('text') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        cfg = self._get_config()
        webhook = cfg.get('webhook_url', '')
        if webhook:
            body = {"msgtype": "text", "text": {"content": str(content)[:4000]}}
            try:
                resp = _json.loads(_ur.urlopen(_ur.Request(
                    webhook, data=_json.dumps(body).encode(),
                    headers={'Content-Type': 'application/json'}
                )).read())
                if resp.get('errcode', -1) != 0:
                    return {'success': False, 'error': resp.get('errmsg', _('WeCom send failed'))}
                return {'success': True}
            except Exception as e:
                return {'success': False, 'error': str(e)[:2000]}

        # 企业应用消息（需 corp_id/secret/agent_id/touser）
        corp_id = cfg.get('corp_id', '')
        secret = cfg.get('secret', '')
        agent_id = cfg.get('agent_id', '')
        touser = cfg.get('touser', '') or payload.get('to', '')
        if not corp_id or not secret or not agent_id:
            return {'success': False, 'error': _('WeCom webhook_url or corp credentials are required')}
        try:
            import requests as _req
            resp = _req.get(
                f'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corp_id}&corpsecret={secret}',
                timeout=10
            ).json()
            token = resp.get('access_token', '')
            if not token:
                return {'success': False, 'error': _('WeCom token acquisition failed: {}').format(resp.get('errmsg'))}
            send_resp = _req.post(
                f'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}',
                json={'touser': touser, 'msgtype': 'text', 'agentid': int(agent_id),
                      'text': {'content': str(content)[:2000]}},
                timeout=10
            ).json()
            if send_resp.get('errcode', -1) != 0:
                return {'success': False, 'error': send_resp.get('errmsg', _('WeCom send failed'))}
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}

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
                    "markdown": {"content": _("**{}**\n[Download file]({})").format(filename, file_url)}}
        resp = _json.loads(_ur.urlopen(_ur.Request(webhook,
            data=_json.dumps(body).encode(), headers={'Content-Type': 'application/json'}
        )).read())
        if resp.get('errcode', -1) != 0:
            raise Exception(resp.get('errmsg', _('WeCom push failed')))

    @staticmethod
    def _fetch_as_base64(url):
        data = _ur.urlopen(url).read()
        return base64.b64encode(data).decode()
