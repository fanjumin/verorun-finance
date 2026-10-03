#!/usr/bin/env python3
"""GatewayFacade — 即时通讯(IM)统一对外接入层（B3 职责收敛后纯 IM）。

消费方插件只需：
    from plugins.im_gateway.gateway import gateway

    gateway.send_message(channel='feishu', to=..., content=...)  # IM 消息投递
    gateway.test(channel=..., data=...)                          # 连接测试
    gateway.list_channels()                                      # IM 渠道枚举

社媒 OAuth 授权 / 社媒账号 / 一键多发(publish) 已随职责收敛迁回 social_push 插件，
不再经本网关；本网关仅保留 IM 适配器投递与跨 worker 频控。
"""
import logging

from i18n import _

from .adapters import get_adapter, list_channels as _im_list_channels

logger = logging.getLogger(__name__)

# 每渠道频控：计数存于 PG rate_limit_events 表，保证 gunicorn 多 worker 下全局生效
_RATE = {'max_calls': 20, 'window': 60}


class GatewayFacade:
    """即时通讯统一门面"""

    # ── 通道列举 ──
    def list_channels(self) -> list:
        """全部 IM 渠道（现有 adapters 注册表）"""
        return [
            {'channel': c, 'channel_type': 'im', 'auth_mode': 'manual',
             'supports_test': False}
            for c in _im_list_channels()
        ]

    # ── 连接测试 ──
    def test(self, channel: str, data: dict) -> tuple:
        """连接测试：委派对应 IM 适配器"""
        adapter = get_adapter(channel)
        if adapter is None:
            return False, _('Unsupported channel: {}').format(channel)
        return adapter.test_connection(data)

    # ── 投递 ──
    def send_message(self, channel: str, to, content: str, **kw) -> dict:
        """单渠道 IM 消息投递"""
        return self._dispatch(channel, {'to': to, 'content': content}, kw)

    def _dispatch(self, channel, payload, kw) -> dict:
        if self._rate_limited(channel):
            return {'success': False, 'error': f'Rate limited for channel: {channel}'}
        adapter = get_adapter(channel)
        if adapter is None:
            return {'success': False, 'error': f'Unsupported channel: {channel}'}
        try:
            result = adapter.send(payload, **kw)
            return result if isinstance(result, dict) else {'success': bool(result)}
        except Exception as e:
            logger.exception('[Gateway] dispatch failed: %s', channel)
            return {'success': False, 'error': str(e)[:2000]}

    def _rate_limited(self, channel: str) -> bool:
        """跨 worker 频控：PG 窗口计数（rate_limit_events 表）。

        多 gunicorn worker 共享同一 PG 计数，60s 窗口内每渠道限 20 次；
        表由 models.init_im_db() 幂等创建。
        """
        from .models import get_im_db
        window_seconds = _RATE['window']
        try:
            with get_im_db() as conn:
                conn.execute(
                    "DELETE FROM rate_limit_events "
                    "WHERE ts < NOW() - INTERVAL %s", (f'{window_seconds} seconds',))
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM rate_limit_events WHERE channel=%s",
                    (channel,)).fetchone()
                if row['c'] >= _RATE['max_calls']:
                    return True
                conn.execute(
                    "INSERT INTO rate_limit_events (channel) VALUES (%s)", (channel,))
                return False
        except Exception:
            logger.exception('[Gateway] rate limit check failed; allow request')
            return False


gateway = GatewayFacade()
