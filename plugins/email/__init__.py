#!/usr/bin/env python3
"""
Email Service Plugin — 邮件服务插件（完全独立）
================================================
统一的邮件服务：SMTP 发信 + IMAP 收信 + 附件 + 已发送记录。
- 独立数据库：PG schema `email`（不依赖主库）
- 独立配置：环境变量 + plugin.json 默认值（不依赖 system_config）
- 独立 i18n：插件自带翻译文件
"""

from i18n import _
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin, clear_plugin_yaml_cache

# 模块级 i18n 引用，由 on_enable 注入
_t = lambda text: text


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class EmailPlugin(BasePlugin):
    name = 'email'
    version = '1.2.0'
    description = 'Email Service — SMTP/IMAP email client with inbox, compose, attachments, and contact management'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立 PG schema: email"""
        from .models import init_email_db
        init_email_db()
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n（幂等）"""
        from .models import init_email_db
        init_email_db()
        init_i18n(self.t)
        print(_('[EmailPlugin] ✅ Email service plugin enabled (PG schema: email)'))
        return True

    def register_routes(self):
        """注册 Flask 路由"""
        from .routes import email_bp
        return [email_bp]

    def on_disable(self, registry):
        """禁用时清理"""
        print(_('[EmailPlugin] ⚠️ Email service plugin disabled'))
        return True

    def on_uninstall(self, registry=None):
        """卸载时清理独立 PG schema（§4.2/§12.5 零残留）

        注意：PluginManager.uninstall() 以无参方式调用本方法，
        故签名必须使用 registry=None 默认值，避免 TypeError 被静默吞掉。
        """
        try:
            from plugins._base.db import get_raw_connection
            conn = get_raw_connection()
            cur = conn.cursor()
            cur.execute("DROP SCHEMA IF EXISTS email CASCADE")
            conn.commit()
            conn.close()
            clear_plugin_yaml_cache('email')
            print(_('[EmailPlugin] ✅ PG schema email dropped on uninstall'))
        except Exception as e:
            print(_('[EmailPlugin] ⚠️ Uninstall cleanup warning: {}').format(e))
        return True

    def get_dashboard_stats(self):
        """Dashboard 统计：已发送邮件总数、联系人总数（§2.3/§6.3）。"""
        try:
            from .models import get_email_db
            with get_email_db() as db:
                total_sent = db.execute("SELECT COUNT(*) AS count FROM email_sent").fetchone()['count']
                total_contacts = db.execute(
                    "SELECT COUNT(DISTINCT to_addr) AS count FROM email_sent"
                ).fetchone()['count']
            return {'total_sent': total_sent, 'total_contacts': total_contacts}
        except Exception:
            return {'total_sent': 0, 'total_contacts': 0}

    def get_schema_version(self):
        """返回当前 schema 版本（§10.6）。"""
        return '1.2.0'

    def migrate(self, from_version, to_version):
        """版本升级逻辑（§10.6）。当前 schema 无历史迁移需求，直接放行。"""
        return True