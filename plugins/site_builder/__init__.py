#!/usr/bin/env python3
"""Site Builder Plugin (v2.1.0) — AI 智能建站插件

v2.1.0 解耦：
  - 建站能力从核心模块 site_builder/ 解耦至此插件。
  - 数据表（site_builder_prompts / site_builder_tasks / design_tokens /
    site_versions）存储于独立数据库 `site_builder` 的 `site_builder` schema，
    安装时自动建库（幂等），卸载时保留数据。
  - 主库共享数据（cms_blocks / cms_posts / 品牌设置）改经 main_site 内部
    API（/api/internal/*）访问（见 internal_client.py）。
  - 含两个子模块：
      site_builder/            → 提示词模板 + 建站任务 + 口令控制台集成
      site_builder/site_settings/ → 统一设计令牌系统

元数据以 plugin.json（plugin_info）为唯一数据源，类内仅保留兜底值。
"""
import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
if os.path.isdir(_ROOT) and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_AUTH_CENTER = os.path.join(_ROOT, 'auth-center')
if os.path.isdir(_AUTH_CENTER) and _AUTH_CENTER not in sys.path:
    sys.path.insert(0, _AUTH_CENTER)

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger

logger = get_plugin_logger('site_builder')


class SiteBuilderPlugin(BasePlugin):
    """AI Site Builder — LLM 驱动的站内网页一键建站插件."""

    @property
    def name(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'name', None) or 'AI Site Builder'

    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '2.1.0'

    @property
    def description(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'description', None) or (
            'LLM-driven website builder: prompt templates, site tasks & '
            'unified design tokens (decoupled from core)'
        )

    @property
    def author(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'author', None) or 'VeroRun'

    def setup(self):
        super().setup()
        self._init_db()

    def on_install(self, registry=None) -> bool:
        self._init_db()
        return True

    def on_enable(self, registry=None) -> bool:
        self._init_db()
        return True

    def _init_db(self):
        """独立库 schema 初始化（幂等，安全重复执行）。

        顺序：
          1. run_migrations()   —— 确保独立库 site_builder 的
                                  site_builder schema 存在。
          2. models.init_tables() + seed_default_prompts()
                                  —— 提示词模板 / 建站任务建表并植入内置模板。
          3. site_settings.models.init_tables()
                                  —— design_tokens / site_versions 建表。
        """
        try:
            from .migrate import run_migrations
            run_migrations()
        except Exception as e:
            logger.warning('[SiteBuilder] schema migration skipped: %s', e)

        try:
            from .models import init_tables, seed_default_prompts
            init_tables()
            seed_default_prompts()
        except Exception as e:
            logger.warning('[SiteBuilder] init_tables/seed failed: %s', e)

        try:
            from .site_settings.models import init_tables as _init_settings_tables
            _init_settings_tables()
        except Exception as e:
            logger.warning('[SiteBuilder] site_settings init_tables failed: %s', e)

    def register_routes(self):
        """挂载 2 个蓝图（url_prefix 各自自定义，由 mount_all_routes 沿用）。

        - /admin/site-builder/*   建站任务 + 提示词模板 + 预览/发布
        - /admin/site-settings/*  统一站点设置（设计令牌）
        """
        from .routes import site_builder_bp
        from .site_settings import register_site_settings_bp
        site_settings_bp = register_site_settings_bp()
        return [site_builder_bp, site_settings_bp]

    def get_schema_version(self) -> str:
        """Read current schema version（标准 §10.6）。"""
        return '2.1.0'

    def migrate(self, from_version: str, to_version: str) -> bool:
        """Run schema migrations（标准 §10.6）。"""
        try:
            self._init_db()
            return True
        except Exception as e:
            logger.error('[SiteBuilder] migrate failed: %s', e)
            return False

    def on_uninstall(self, registry=None) -> bool:
        """卸载：保留独立库数据（site_builder schema 不被删除），仅移除 registry 记录。"""
        self.log('site_builder uninstalled: independent DB schema/data preserved')
        return True

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读独立库 site_builder schema，幂等）。"""
        stats = {'total_tasks': 0, 'completed_tasks': 0, 'total_prompts': 0}
        try:
            from .models import get_db
            with get_db() as conn:
                tasks = conn.execute('SELECT COUNT(*) AS c FROM site_builder_tasks').fetchone()
                done = conn.execute(
                    "SELECT COUNT(*) AS c FROM site_builder_tasks WHERE status='completed'"
                ).fetchone()
                prompts = conn.execute('SELECT COUNT(*) AS c FROM site_builder_prompts').fetchone()
            stats['total_tasks'] = int(tasks['c']) if tasks else 0
            stats['completed_tasks'] = int(done['c']) if done else 0
            stats['total_prompts'] = int(prompts['c']) if prompts else 0
        except Exception as e:
            logger.warning('[SiteBuilder] get_dashboard_stats failed: %s', e)
        return stats
