#!/usr/bin/env python3
"""
IM Gateway Plugin — 即时通讯网关插件
======================================
独立数据库 im_gateway.db

统一管理即时通讯频道（飞书 / 企业微信 / QQ / 钉钉）的凭据配置、
连接测试与消息/媒体推送，通过 adapter 基类抽象，便于扩展 Telegram / LINE。
"""

from i18n import _
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger
from .models import init_im_db, migrate_from_main_db

logger = get_plugin_logger('im_gateway')

# 模块级 i18n 引用，由 on_enable 注入
_t = lambda text: text


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class ImGatewayPlugin(BasePlugin):
    name = 'im_gateway'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = ('IM Gateway — Unified IM messaging channel gateway '
                   '(Feishu / WeCom / DingTalk / QQ / Telegram / LINE) '
                   'plus web third-party login')
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立数据库 + 从主库迁移已有频道配置"""
        init_im_db()
        try:
            n = migrate_from_main_db()
            if n:
                logger.info(f'Migrated {n} channel configurations from main database')
        except Exception as e:
            logger.warning(f'Channel configuration migration warning: {e}')
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n（幂等）"""
        init_im_db()
        init_i18n(self.t)
        logger.info('IM gateway plugin enabled')
        return True

    def register_routes(self):
        """注册 Flask 路由（IM 频道配置 + 入站 Webhook + 聚合概览 + Web 第三方登录提供方/闭环）"""
        from .routes import im_bp
        from .routes_webhook import webhook_bp
        from .routes_overview import overview_bp
        from .routes_login import login_bp
        from .routes_third_login import third_login_bp
        return [im_bp, webhook_bp, overview_bp, login_bp, third_login_bp]

    def register_jobs(self):
        """社媒 token 刷新已随社媒职责迁至 social_push；本插件无定时任务。"""
        return []

    # ── 事件消费：把内核告警推到 IM ──

    def get_event_handlers(self):
        """订阅内核事件（管理器 activate 时自动 add_action，见 plugin_manager/manager.py:1122）。

        当前订阅 stock.alert.triggered —— 由 stock_analysis.alert_engine.scheduled_scan
        以 `do_action("stock.alert.triggered", ev)` **位置参数**派发（hooks.do_action 原样透传），
        故处理器必须接受位置事件体。ev 结构见该插件 alert_engine._trigger()：
        {alert_id, symbol, name, type, threshold, observed, message, channels, at}。
        """
        def _on_stock_alert(ev=None, **kwargs):
            self.push_alert(ev if isinstance(ev, dict) else dict(kwargs))

        return {'stock.alert.triggered': _on_stock_alert}

    def push_alert(self, ev: dict) -> list:
        """把告警事件推送到全部「已启用」IM 频道，返回逐频道结果（供日志/排障）。

        仅在告警自身的 channels 含 'im' 时推送 —— 尊重用户在该条告警上选的渠道
        （stock_analysis 的 CHANNEL_CODES = in_app/email/im），不把只勾了「站内信」的
        告警擅自发到 IM。频道未配置时各适配器会返回失败原因，此处仅记日志不抛错。
        """
        results = []
        if not ev or 'im' not in (ev.get('channels') or []):
            return results
        text = self._format_alert_text(ev)
        for channel in self._enabled_channels():
            try:
                from .gateway import gateway
                # to='' → 各适配器回落到自身配置的默认接收人（飞书 admin_open_id/chat_id、企微 touser）
                r = gateway.send_message(channel=channel, to='', content=text)
            except Exception as e:
                r = {'success': False, 'error': str(e)}
            if not (isinstance(r, dict) and r.get('success')):
                logger.warning('alert push failed channel=%s: %s', channel, r)
            results.append({'channel': channel, 'result': r})
        return results

    @staticmethod
    def _format_alert_text(ev: dict) -> str:
        """告警纯文本（各 IM 适配器均支持 text；长度截断由适配器各自负责）"""
        symbol = ev.get('symbol') or ''
        name = ev.get('name') or symbol
        lines = ['【VeroRun 告警】', f'{name} ({symbol})'.strip()]
        if ev.get('message'):
            lines.append(str(ev['message']))
        if ev.get('observed') is not None:
            line = f'当前值: {ev["observed"]}'
            if ev.get('threshold') is not None:
                line += f' / 阈值: {ev["threshold"]}'
            lines.append(line)
        if ev.get('at'):
            lines.append(f'时间: {ev["at"]}')
        return '\n'.join(lines)

    @staticmethod
    def _enabled_channels() -> list:
        """channel_configs 中 is_enabled=1 且属 IM 适配器的频道标识"""
        try:
            from .adapters import list_channels as _im_channels
            im = set(_im_channels())
        except Exception:
            im = set()
        try:
            from .models import get_im_db
            with get_im_db() as conn:
                rows = conn.execute(
                    'SELECT channel FROM channel_configs WHERE is_enabled=1'
                ).fetchall()
            return [r['channel'] for r in rows if r['channel'] in im]
        except Exception as e:
            logger.warning('load enabled channels failed: %s', e)
            return []

    def on_disable(self, registry):
        """禁用时清理（本插件无进程内调度器，社媒 token 刷新已迁至 social_push）"""
        logger.warning('IM gateway plugin disabled')
        return True

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读插件独立库 channel_configs，幂等）。"""
        stats = {'total_channels': 0, 'enabled_channels': 0}
        try:
            from .models import get_im_db
            with get_im_db() as conn:
                total = conn.execute('SELECT COUNT(*) AS c FROM channel_configs').fetchone()
                enabled = conn.execute(
                    'SELECT COUNT(*) AS c FROM channel_configs WHERE is_enabled=1'
                ).fetchone()
            stats['total_channels'] = int(total['c']) if total else 0
            stats['enabled_channels'] = int(enabled['c']) if enabled else 0
        except Exception:
            logger.warning('get_dashboard_stats failed', exc_info=True)
        return stats

    def on_uninstall(self, registry):
        """卸载清理（职责收敛后：仅清 IM 运行表，保留第三方登录数据）。

        - 删除 IM 运行表：channel_configs / rate_limit_events；
        - 显式保留第三方登录三表 login_providers / login_user_bindings /
          oauth_login_states —— 联邦登录绑定属用户资产，卸载 IM 频道不应连带清除。
        """
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP TABLE IF EXISTS im_gateway.rate_limit_events CASCADE')
                cur.execute('DROP TABLE IF EXISTS im_gateway.channel_configs CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('im_gateway IM tables dropped; third-party login tables preserved')
        except Exception as e:
            logger.error('on_uninstall cleanup failed: %s', e, exc_info=True)
        return True

    # ── 对外接口：供主系统（媒体库）调用推送 ──

    def push_media(self, channel, file_url, filename, mime):
        """向指定频道推送媒体文件。

        供主系统 media_library_push 调用。插件禁用时该实例不存在，
        主系统需据此提示_("IM Gateway is not enabled")。

        Args:
            channel: 'feishu' | 'wecom'
            file_url: 文件可访问 URL
            filename: 文件名
            mime: MIME 类型

        Raises:
            Exception: 频道未配置或推送失败
        """
        from .adapters import get_adapter
        adapter = get_adapter(channel)
        if adapter is None:
            raise Exception(_('Channel {channel} does not support media push').format(channel=channel))
        adapter.push_media(file_url, filename, mime)
