#!/usr/bin/env python3
"""IM Gateway — 企业微信适配器

迁移自 auth-center/routes/admin.py。修复 channel_name → channel 列名 bug。
批次 D1-b：出站统一走 http_client（超时 + access_token TTL 缓存）；
          push_media 修正「假媒体」——图片写入真实 md5，文件类型不再伪造 media_id。
"""
from i18n import _
import os
import json as _json
import base64
import hashlib

from .base import BaseIMAdapter
from .. import http_client

_TOKEN_URL = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken'
_SEND_URL = 'https://qyapi.weixin.qq.com/cgi-bin/message/send'

# 企业微信群机器人图片：base64 编码前原始大小上限 2MB
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
# access_token 缓存（有效期 7200s，提前 5 分钟过期）
_token_cache = http_client.TTLCache()


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

    # ── token（进程内 TTL 缓存） ──

    def _get_token(self, corp_id, secret):
        cache_key = ('wecom', corp_id)
        cached = _token_cache.get(cache_key)
        if cached:
            return cached
        _, rd = http_client.request_json(
            'GET', _TOKEN_URL,
            params={'corpid': corp_id, 'corpsecret': secret}
        )
        token = rd.get('access_token', '')
        if not token:
            raise Exception(_('WeCom token acquisition failed: {}').format(rd.get('errmsg')))
        ttl = int(rd.get('expires_in', 7200)) - 300
        _token_cache.set(cache_key, token, ttl if ttl > 60 else 6900)
        return token

    def test_connection(self, data):
        corp_id = (data.get('corp_id') or '').strip()
        secret = (data.get('secret') or '').strip()
        if not corp_id or not secret:
            return False, _('Enterprise ID and Secret cannot be empty')
        try:
            _, rd = http_client.request_json(
                'GET', _TOKEN_URL,
                params={'corpid': corp_id, 'corpsecret': secret}
            )
            if rd.get('access_token'):
                return True, _('WeCom connection successful!')
            return False, _('WeCom returned: {} (errcode={})').format(
                rd.get('errmsg', 'unknown'), rd.get('errcode'))
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
                _, resp = http_client.request_json('POST', webhook, json=body)
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
            token = self._get_token(corp_id, secret)
            _, send_resp = http_client.request_json(
                'POST', _SEND_URL,
                params={'access_token': token},
                json={'touser': touser, 'msgtype': 'text', 'agentid': int(agent_id),
                      'text': {'content': str(content)[:2000]}}
            )
            if send_resp.get('errcode', -1) != 0:
                return {'success': False, 'error': send_resp.get('errmsg', _('WeCom send failed'))}
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}

    # ── 媒体推送 ──

    def push_media(self, file_url, filename, mime):
        cfg = self._get_config()
        webhook = cfg.get('webhook_url', '')
        if not webhook:
            raise Exception(_("WeCom webhook_url is empty"))

        if mime.startswith('image/'):
            raw = self._fetch_bytes(file_url)
            if len(raw) > _MAX_IMAGE_BYTES:
                raise Exception(_('WeCom image exceeds the 2MB limit'))
            body = {"msgtype": "image", "image": {
                "base64": base64.b64encode(raw).decode(),
                "md5": hashlib.md5(raw).hexdigest(),
            }}
        else:
            # 群机器人不支持 file/video/audio 消息类型（media_id 需企业应用上传）。
            # 如实降级为「下载链接」，不再伪造 media_id 假装成功。
            body = {"msgtype": "markdown",
                    "markdown": {"content": _("**{}**\n[Download file]({})").format(filename, file_url)}}

        _, resp = http_client.request_json('POST', webhook, json=body)
        if resp.get('errcode', -1) != 0:
            raise Exception(resp.get('errmsg', _('WeCom push failed')))

    @staticmethod
    def _fetch_bytes(url):
        """SSRF 安全下载（内网拦截 + 超时 + 体积上限）。"""
        return http_client.safe_fetch(url, max_bytes=_MAX_IMAGE_BYTES)