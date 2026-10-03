#!/usr/bin/env python3
"""IM Gateway — 钉钉适配器

迁移自 auth-center/routes/admin.py。使用 appkey/appsecret 获取 access_token 测试连接。
批次 D1-b：补齐真实出站 send()——OAPI 工作通知（corpconversation/asyncsend_v2）；
          出站统一走 http_client（超时 + access_token TTL 缓存）。
"""
from i18n import _

from .base import BaseIMAdapter
from .. import http_client
import json as _json

_TOKEN_URL = 'https://oapi.dingtalk.com/gettoken'
_ASYNC_SEND_URL = 'https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2'

_token_cache = http_client.TTLCache()


class DingTalkAdapter(BaseIMAdapter):
    channel = 'dingtalk'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'app_key', 'label': 'AppKey', 'type': 'text'},
            {'key': 'app_secret', 'label': 'AppSecret', 'type': 'password'},
            {'key': 'agent_id', 'label': 'AgentId', 'type': 'text'},
            {'key': 'corp_id', 'label': 'CorpId', 'type': 'text'},
            {'key': 'default_userid', 'label': 'Default Recipient UserID', 'type': 'text'},
        ]

    # ── token（进程内 TTL 缓存） ──

    def _get_token(self, app_key, app_secret):
        cache_key = ('dingtalk', app_key)
        cached = _token_cache.get(cache_key)
        if cached:
            return cached
        _, rd = http_client.request_json(
            'GET', _TOKEN_URL,
            params={'appkey': app_key, 'appsecret': app_secret}
        )
        token = rd.get('access_token', '')
        if not token:
            raise Exception(_('DingTalk token acquisition failed: {}').format(rd.get('errmsg')))
        ttl = int(rd.get('expires_in', 7200)) - 300
        _token_cache.set(cache_key, token, ttl if ttl > 60 else 6900)
        return token

    def test_connection(self, data):
        app_key = (data.get('app_key') or '').strip() or (data.get('appId') or '').strip()
        app_secret = (data.get('app_secret') or '').strip() or (data.get('appSecret') or '').strip()
        if not app_key or not app_secret:
            return False, _('AppKey and AppSecret cannot be empty')
        try:
            _, rd = http_client.request_json(
                'GET', _TOKEN_URL,
                params={'appkey': app_key, 'appsecret': app_secret}
            )
            if rd.get('access_token'):
                return True, _('DingTalk connection successful!')
            return False, _('DingTalk returned: {} (errcode={})').format(
                rd.get('errmsg', 'unknown'), rd.get('errcode'))
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

    # ── 消息发送（OAPI 工作通知） ──

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='dingtalk' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("DingTalk channel is not configured"))
        return _json.loads(row['config_json'])

    def send(self, payload: dict, **kw) -> dict:
        """通过企业应用工作通知发送文本：需 agent_id + 接收人 userid。"""
        content = payload.get('content') or payload.get('text') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        cfg = self._get_config()
        app_key = cfg.get('app_key', '')
        app_secret = cfg.get('app_secret', '')
        agent_id = cfg.get('agent_id', '')
        if not app_key or not app_secret:
            return {'success': False, 'error': _('AppKey and AppSecret cannot be empty')}
        if not agent_id:
            return {'success': False, 'error': _('DingTalk AgentId is required')}
        userid = str(payload.get('to') or cfg.get('default_userid') or cfg.get('touser') or '')
        if not userid:
            return {'success': False, 'error': _('DingTalk recipient (userid) is required')}
        try:
            token = self._get_token(app_key, app_secret)
            # msg 按官方文档以 JSON 字符串传（asyncsend_v2 约定）
            body = {
                'agent_id': int(agent_id),
                'userid_list': userid,
                'msg': _json.dumps({'msgtype': 'text', 'text': {'content': str(content)[:2000]}}),
            }
            _, rd = http_client.request_json(
                'POST', _ASYNC_SEND_URL,
                params={'access_token': token},
                json=body
            )
            if rd.get('errcode', -1) != 0:
                return {'success': False, 'error': rd.get('errmsg', _('DingTalk send failed'))}
            return {'success': True, 'task_id': rd.get('task_id')}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}