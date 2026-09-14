#!/usr/bin/env python3
"""
Social Push Plugin — 社媒推广插件
==================================
独立 PG schema `social_push`

多平台社媒内容发布（微信公众号 / 微博 / 今日头条），含发布历史。
对外暴露 publish_to_platform() / PLATFORM_INFO，供 content_factory、
cms_admin 等主系统模块经 plugin_manager 调用。

注意：本插件为 Phase 2 物理解耦产物，LLM 调用（AI 文案/配图）暂保持原状，
      将在 Phase 3 下沉到 agent_matrix 内核。
"""

from i18n import _

from plugin_manager.base import BasePlugin
from .models import init_sp_db, migrate_from_main_db, get_sp_db

# 模块级 i18n 引用，由 on_enable 注入
_t = lambda text: text


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class SocialPushPlugin(BasePlugin):
    name = 'social_push'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Social Push — Multi-platform social content publishing'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立数据库 + 从主库迁移历史发布记录"""
        init_sp_db()
        try:
            n = migrate_from_main_db()
            if n:
                print(f'[SocialPushPlugin] ✅ Migrated {n} publication records from main database')
        except Exception as e:
            print(f'[SocialPushPlugin] ⚠️ Migration warning for publication records: {e}')
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n（幂等）"""
        init_sp_db()
        init_i18n(self.t)
        print(_('[SocialPushPlugin] ✅ Social promotion plugin is enabled'))
        return True

    def register_routes(self):
        """注册 Flask 路由（社媒发布 API）"""
        from .routes import social_bp
        return [social_bp]

    def register_jobs(self):
        """注册 APScheduler 定时任务（P4：发布队列到期派发）"""
        from .scheduler import get_jobs
        return get_jobs(self)

    def on_disable(self, registry):
        """禁用时清理"""
        print(_('[SocialPushPlugin] ⚠️ Social promotion plugin is disabled'))
        return True

    # ── 对外接口：供主系统（content_factory / cms_admin）调用 ──

    @property
    def PLATFORM_INFO(self):
        """平台元信息，供 cms_admin 判定频道是否为社媒平台"""
        from .routes import PLATFORM_INFO
        return PLATFORM_INFO

    def publish_to_platform(self, platform, title, body, body_html, summary,
                            author, cover_image_url, auto_publish, admin_id):
        """发布到单个社媒平台。供主系统跨模块调用。

        Returns: {platform, status, media_id, message, error}
        """
        from .routes import _publish_to_platform
        return _publish_to_platform(
            platform=platform, title=title, body=body, body_html=body_html,
            summary=summary, author=author, cover_image_url=cover_image_url,
            auto_publish=auto_publish, admin_id=admin_id,
        )

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读插件独立库 social_push_logs，幂等）。"""
        try:
            with get_sp_db() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM social_push_logs"
                ).fetchone()
                today = conn.execute(
                    "SELECT COUNT(*) AS c FROM social_push_logs "
                    "WHERE created_at::timestamptz >= CURRENT_DATE"
                ).fetchone()
            return {
                'total_publishes': int(total['c']) if total else 0,
                'today_publishes': int(today['c']) if today else 0,
            }
        except Exception:
            return {'total_publishes': 0, 'today_publishes': 0}

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 social_push schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS social_push CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            print(_('[SocialPushPlugin] social_push schema dropped'))
        except Exception as e:
            print(f'[SocialPushPlugin] on_uninstall cleanup failed: {e}')
        return True
