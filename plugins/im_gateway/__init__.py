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
    version = '1.2.0'
    description = 'IM Gateway — Unified instant-messaging channel gateway'
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
        """注册 Flask 路由（管理端频道配置 API）"""
        from .routes import im_bp
        return [im_bp]

    def on_disable(self, registry):
        """禁用时清理"""
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
        """F-010: 卸载清理 — 删除 im_gateway schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS im_gateway CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('im_gateway schema dropped')
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
            raise Exception(f'Channel {channel} does not support media push')
        adapter.push_media(file_url, filename, mime)
