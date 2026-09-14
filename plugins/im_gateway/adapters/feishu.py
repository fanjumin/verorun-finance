#!/usr/bin/env python3
"""IM Gateway — 飞书适配器

迁移自 auth-center/routes/admin.py 的频道测试与媒体推送逻辑。
修复原代码 channel_name → channel 的列名 bug。
"""
import os
import json as _json
import urllib.request as _ur

from i18n import _

from .base import BaseIMAdapter


class FeishuAdapter(BaseIMAdapter):
    channel = 'feishu'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'app_id', 'label': 'App ID', 'type': 'text'},
            {'key': 'app_secret', 'label': 'App Secret', 'type': 'password'},
            {'key': 'admin_open_id', 'label': 'Admin Open ID', 'type': 'text'},
            {'key': 'verification_token', 'label': 'Verification Token', 'type': 'password'},
            {'key': 'encrypt_key', 'label': 'Encrypt Key', 'type': 'password'},
        ]

    def test_connection(self, data):
        app_id = (data.get('app_id') or '').strip()
        app_secret = (data.get('app_secret') or '').strip()
        if not app_id or not app_secret:
            return False, _('App ID and App Secret cannot be empty')
        try:
            import requests as _req
            resp = _req.post(
                'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                json={'app_id': app_id, 'app_secret': app_secret},
                timeout=10
            )
            rd = resp.json()
            if rd.get('code') == 0:
                return True, _('Feishu connection successful!')
            return False, _("Feishu returned error: {} (code={})").format(rd.get('msg', _('Unknown')), rd.get('code'))
        except Exception as e:
            return False, _('Connection failed: {}').format(str(e))

    def get_env_fallback(self):
        cfg = {}
        app_id = os.environ.get('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        admin_id = os.environ.get('FEISHU_ADMIN_OPEN_ID', '')
        verify_token = os.environ.get('FEISHU_VERIFICATION_TOKEN', '')
        encrypt_key = os.environ.get('FEISHU_ENCRYPT_KEY', '')
        if app_id:
            cfg['app_id'] = app_id
        if app_secret:
            cfg['app_secret'] = self._mask(app_secret)
        if admin_id:
            cfg['admin_open_id'] = admin_id
        if verify_token:
            cfg['verification_token'] = self._mask(verify_token)
        if encrypt_key:
            cfg['encrypt_key'] = self._mask(encrypt_key)
        return cfg

    # ── 消息发送 ──

    def send(self, payload: dict, **kw) -> dict:
        """发送纯文本消息：优先 payload['to']，否则用配置的 admin_open_id / chat_id。"""
        content = payload.get('content') or payload.get('text') or ''
        if not content:
            return {'success': False, 'error': _('Message content is empty')}
        cfg = self._get_config()
        app_id = cfg.get('app_id', '')
        app_secret = cfg.get('app_secret', '')
        if not app_id or not app_secret:
            return {'success': False, 'error': _('Feishu App ID or App Secret is empty')}
        try:
            token_resp = _json.loads(_ur.urlopen(
                _ur.Request('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                            data=_json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode(),
                            headers={'Content-Type': 'application/json'})
            ).read())
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}
        token = token_resp.get('tenant_access_token', '')
        if not token:
            return {'success': False, 'error': _('Feishu token acquisition failed: {}').format(str(token_resp))}

        receive_id = str(payload.get('to') or cfg.get('admin_open_id') or cfg.get('chat_id') or '')
        if not receive_id:
            return {'success': False, 'error': _('Feishu receive_id is not configured')}
        receive_id_type = 'chat_id' if receive_id.startswith('oc_') else 'open_id'
        body = {'receive_id': receive_id, 'msg_type': 'text',
                'content': _json.dumps({'text': str(content)[:2000]})}
        try:
            resp = _json.loads(_ur.urlopen(_ur.Request(
                'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=' + receive_id_type,
                data=_json.dumps(body).encode(),
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
            )).read())
            if resp.get('code', -1) != 0:
                return {'success': False, 'error': resp.get('msg', _('Feishu message sending failed'))}
            return {'success': True, 'message_id': (resp.get('data') or {}).get('message_id')}
        except Exception as e:
            return {'success': False, 'error': str(e)[:2000]}

    # ── 媒体推送 ──

    def _get_config(self):
        from plugins.im_gateway.models import get_im_db
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel='feishu' AND is_enabled=1 LIMIT 1"
            ).fetchone()
        if not row or not row['config_json']:
            raise Exception(_("Feishu channel is not configured"))
        return _json.loads(row['config_json'])

    def push_media(self, file_url, filename, mime):
        cfg = self._get_config()
        app_id = cfg.get('app_id', '')
        app_secret = cfg.get('app_secret', '')
        if not app_id or not app_secret:
            raise Exception(_("Feishu App ID or App Secret is empty"))
        token_resp = _json.loads(_ur.urlopen(
            _ur.Request('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                        data=_json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
                        headers={'Content-Type': 'application/json'})
        ).read())
        token = token_resp.get('tenant_access_token', '')
        if not token:
            raise Exception(_("Feishu token acquisition failed: {}").format(str(token_resp)))

        chat_id = cfg.get('chat_id', '')
        if not chat_id:
            raise Exception(_("Feishu group chat_id is not configured"))

        if mime.startswith('image/'):
            body = {
                "receive_id": chat_id, "msg_type": "image",
                "content": _json.dumps({"image_key": self._upload_image(token, file_url)})
            }
        elif mime.startswith('video/') or mime.startswith('audio/'):
            body = {
                "receive_id": chat_id, "msg_type": "file",
                "content": _json.dumps({"file_key": self._upload_file(token, file_url, filename, mime)})
            }
        else:
            card = {
                "header": {"title": {"tag": "plain_text", "content": _("Media file")}, "template": "blue"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md",
                     "content": "**{}**".format(filename)}},
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": _("Download")},
                         "type": "primary", "url": file_url}
                    ]}
                ]
            }
            body = {"receive_id": chat_id, "msg_type": "interactive", "content": _json.dumps(card)}

        url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id'
        resp = _json.loads(_ur.urlopen(_ur.Request(url,
            data=_json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        )).read())
        if resp.get('code', -1) != 0:
            raise Exception(resp.get('msg', _('Feishu message sending failed')))

    def _upload_image(self, token, file_url):
        img_data = _ur.urlopen(file_url).read()
        boundary = '----FormBoundary7MA4YWxkTrZu0gW'
        body = (b'--' + boundary.encode() + b'\r\n'
                b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
                b'--' + boundary.encode() + b'\r\n'
                b'Content-Disposition: form-data; name="image"; filename="image"\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n' + img_data + b'\r\n'
                b'--' + boundary.encode() + b'--\r\n')
        resp = _json.loads(_ur.urlopen(_ur.Request(
            'https://open.feishu.cn/open-apis/im/v1/images', data=body,
            headers={'Authorization': 'Bearer ' + token,
                     'Content-Type': 'multipart/form-data; boundary=' + boundary}
        )).read())
        if resp.get('code', -1) != 0:
            raise Exception(_("Image upload failed: {}").format(resp.get('msg', '')))
        return resp['data']['image_key']

    def _upload_file(self, token, file_url, filename, mime):
        file_data = _ur.urlopen(file_url).read()
        boundary = '----FormBoundary7MA4YWxkTrZu0gW'
        file_type = 'mp4' if mime.startswith('video/') else 'opus'
        body = (b'--' + boundary.encode() + b'\r\n'
                b'Content-Disposition: form-data; name="file_type"\r\n\r\n' +
                file_type.encode() + b'\r\n'
                b'--' + boundary.encode() + b'\r\n'
                b'Content-Disposition: form-data; name="file_name"\r\n\r\n' +
                filename.encode() + b'\r\n'
                b'--' + boundary.encode() + b'\r\n'
                b'Content-Disposition: form-data; name="file"; filename="' +
                filename.encode() + b'"\r\n'
                b'Content-Type: application/octet-stream\r\n\r\n' + file_data + b'\r\n'
                b'--' + boundary.encode() + b'--\r\n')
        resp = _json.loads(_ur.urlopen(_ur.Request(
            'https://open.feishu.cn/open-apis/im/v1/files', data=body,
            headers={'Authorization': 'Bearer ' + token,
                     'Content-Type': 'multipart/form-data; boundary=' + boundary}
        )).read())
        if resp.get('code', -1) != 0:
            raise Exception(_("File upload failed: {}").format(resp.get('msg', '')))
        return resp['data']['file_key']
