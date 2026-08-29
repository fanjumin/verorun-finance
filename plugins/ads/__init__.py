#!/usr/bin/env python3
"""
Ad Management Plugin — 广告管理插件
=====================================
数据存储于主库 PostgreSQL 的独立 ads schema（ad_placements / ad_zones / ad_stats / ad_clicks）。
提供 /admin/ads API、管理界面与 AI-Ready 工具（Agent Matrix 集成）。
"""

from i18n import _
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin
from .models import init_ad_db
from .routes import ads_bp

# 模块级 i18n 引用：默认使用真实翻译函数，on_enable 时再注入插件上下文翻译
_t = _


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class AdsPlugin(BasePlugin):
    name = 'ads'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Ad Management — AI-powered ad placements, zones, stats, multi-site, and Agent Matrix integration'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化 ads schema 与表"""
        init_ad_db()
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n（幂等）"""
        init_ad_db()
        init_i18n(self.t)
        print(_('[AdsPlugin] ✅ Advertising management plugin is enabled'))
        return True

    def register_routes(self):
        """注册 Flask 路由"""
        return [ads_bp]

    def get_dashboard_stats(self):
        """Dashboard 统计卡片数据（查询 ads schema 汇总，异常时返回零值）"""
        try:
            from .models import get_ads_db
            with get_ads_db() as conn:
                row = conn.execute('''SELECT
                    COUNT(*) AS total_placements,
                    COALESCE(SUM(impressions), 0) AS total_impressions,
                    COALESCE(SUM(clicks), 0) AS total_clicks,
                    COALESCE(SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END), 0) AS active_placements
                FROM ad_placements''').fetchone()
                return {
                    'total_placements': row['total_placements'],
                    'total_impressions': row['total_impressions'],
                    'total_clicks': row['total_clicks'],
                    'active_placements': row['active_placements'],
                }
        except Exception:
            return {'total_placements': 0, 'total_impressions': 0, 'total_clicks': 0, 'active_placements': 0}

    def on_disable(self, registry):
        """禁用时清理"""
        print(_('[AdsPlugin] ⚠️ Advertising management plugin is disabled'))
        return True

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 ads schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS ads CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            print(_('[AdsPlugin] ads schema dropped'))
        except Exception as e:
            print(f'[AdsPlugin] on_uninstall cleanup failed: {e}')
        return True