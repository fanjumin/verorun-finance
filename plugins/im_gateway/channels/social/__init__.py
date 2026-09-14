#!/usr/bin/env python3
"""社媒渠道适配器集合（Phase 3）。

每个平台 = BaseChannelAdapter 子类，注册到统一渠道注册表。
发布优先 import 复用 plugins/social_push/providers 已稳定 provider；
国内平台（weibo/toutiao）token 来源为网关账号表，适配器内按同构逻辑实现
（逻辑对齐 auth-center/services 同名函数，仅 token 来源改为网关账号）。

import 本包即完成全部平台注册（channels/__init__.py 末尾导入）。
"""
import json

from ..base import BaseChannelAdapter
from .. import register_channel_adapter  # noqa: F401
from ...models_accounts import list_accounts


class _SocialChannelBase(BaseChannelAdapter):
    """社媒渠道公共基类：账号取自 channel_accounts（解密后 config）"""

    channel_type = 'social'
    auth_mode = 'oauth'

    def get_config_fields(self):
        """凭据走 OAuth / 客户端凭据，无手动表单字段"""
        return []

    def _account(self) -> dict:
        """取该平台第一个启用账号的 config（解密后）"""
        accts = list_accounts(self.channel)
        for a in accts:
            if a.get('is_enabled'):
                return a['config']
        raise RuntimeError(f'No connected account for channel: {self.channel}')

    def _publish_params(self, payload: dict) -> dict:
        """统一 payload → social_push publish() 参数"""
        return {
            'title': payload.get('title', ''),
            'body': payload.get('body', ''),
            'summary': payload.get('summary', ''),
            'image_url': payload.get('image_url', ''),
            'link_url': payload.get('link_url', ''),
        }

    def test_connection(self, data):
        try:
            self._account()
            return True, 'Connected'
        except Exception as e:
            return False, str(e)[:200]


def app_credentials(channel: str) -> dict:
    """从 channel_configs 读取该平台 app 凭据（client_id/client_secret/api_key 等）"""
    from ...models import get_im_db
    try:
        with get_im_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM channel_configs WHERE channel=%s", (channel,)
            ).fetchone()
        return json.loads(row['config_json']) if row else {}
    except Exception:
        return {}


# 导入各平台实现（装饰器在 import 时完成注册）
from . import twitter  # noqa: E402,F401
from . import linkedin  # noqa: E402,F401
from . import reddit  # noqa: E402,F401
from . import weibo  # noqa: E402,F401
from . import toutiao  # noqa: E402,F401
from . import facebook  # noqa: E402,F401
from . import instagram  # noqa: E402,F401
from . import wechat_oa  # noqa: E402,F401
from . import telegram_channel  # noqa: E402,F401
