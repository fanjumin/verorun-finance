#!/usr/bin/env python3
"""GatewayFacade — 统一社媒网关对外接入层（消费者单一连接，Phase 0）。

消费方插件只需：
    from plugins.im_gateway.gateway import gateway

    gateway.publish(channels=['twitter', 'linkedin'], payload={...})  # 一发多平台
    gateway.send_message(channel='telegram', to=..., content=...)     # IM 消息
    gateway.connect(platform='twitter')                                # 第三方登录(发起)
    gateway.callback(code=..., state=...)                              # 第三方登录(回调)
    gateway.get_status(task_id=...)                                    # 发布/消息状态
    gateway.test(channel=..., data=...)                                # 连接测试

Phase 0：仅做路由骨架，test/list 同时覆盖现有 IM 适配器（兼容，零改动现有代码）。
后续 Phase 逐步纳入社媒渠道、OAuth、入站事件、状态跟踪。
"""
import logging

from i18n import _

from .channels import get_channel_adapter, list_channels
from .adapters import get_adapter  # 现有 IM 注册表（只读使用，不改动）

logger = logging.getLogger(__name__)

# 每渠道频控（Phase 5 扩展用，Phase 0 即预留）
# 计数存于 PG rate_limit_events 表，保证 gunicorn 多 worker 下全局生效
_RATE = {'max_calls': 20, 'window': 60}


class GatewayFacade:
    """统一网关门面"""

    # ── 通道列举 ──
    def list_channels(self) -> list:
        """全部渠道（现有 IM + 新增渠道）"""
        im = [
            {'channel': c, 'channel_type': 'im', 'auth_mode': 'manual',
             'supports_test': False}
            for c in _im_list()
        ]
        return im + list_channels()

    # ── 连接测试 ──
    def test(self, channel: str, data: dict) -> tuple:
        """连接测试：优先统一注册表，回退现有 IM 适配器"""
        adapter = get_channel_adapter(channel) or get_adapter(channel)
        if adapter is None:
            return False, _('Unsupported channel: {}').format(channel)
        return adapter.test_connection(data)

    # ── 投递 ──
    def publish(self, channels, payload: dict, **kw) -> dict:
        """一发多平台：channels=['twitter','linkedin'] 或 ['telegram']"""
        if isinstance(channels, str):
            channels = [channels]
        results = {}
        for ch in channels:
            results[ch] = self._dispatch(ch, payload, kw)
        return results

    def send_message(self, channel: str, to, content: str, **kw) -> dict:
        """单渠道消息投递"""
        return self._dispatch(channel, {'to': to, 'content': content}, kw)

    def get_status(self, channel: str, task_id: str):
        """发布/消息状态跟踪（未实现状态跟踪的渠道返回 None）"""
        adapter = get_channel_adapter(channel)
        if adapter is None:
            return None
        try:
            return adapter.get_status(task_id)
        except Exception:
            logger.exception('[Gateway] get_status failed: %s', channel)
            return None

    def _dispatch(self, channel, payload, kw) -> dict:
        if self._rate_limited(channel):
            return {'success': False, 'error': f'Rate limited for channel: {channel}'}
        adapter = get_channel_adapter(channel) or get_adapter(channel)
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


def _im_list():
    """现有 IM 适配器标识列表（adapters.list_channels 返回字符串列表）"""
    from .adapters import list_channels as _im_list_channels
    try:
        return _im_list_channels()
    except Exception:
        return []


gateway = GatewayFacade()
