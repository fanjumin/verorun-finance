#!/usr/bin/env python3
"""
Health Check — 系统健康巡检中心 + 插件
============================================
全站自动化健康巡检：可扩展检查框架、定时自动巡检、仪表盘、
异常告警（邮件/站内信/Webhook）、与 Workflow 引擎集成（自动恢复）。
使用 PostgreSQL health schema，8 张表完全自包含。

使用方式:
    from plugins.health_check import health_bp
    app.register_blueprint(health_bp)
"""

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from .routes import health_bp
from .models import init_health_tables, get_db

from plugin_manager.base import BasePlugin

try:
    from plugin_manager.logger import get_plugin_logger
    _logger = get_plugin_logger('health_check')
except ImportError:
    import logging
    _logger = logging.getLogger('health_check')

__all__ = ['health_bp', 'init_health_tables', 'get_db', 'HealthCheckPlugin']


class HealthCheckPlugin(BasePlugin):
    name = 'health_check'
    version = '1.7.0'
    description = 'System Health Check — Automated health monitoring + alerting + trend analysis'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立数据库表"""
        from .models import init_health_tables
        try:
            init_health_tables()
            _logger.info('Database tables initialized (PG health schema)')
        except Exception as e:
            _logger.warning('DB init warning: %s', e)
        return True

    def on_enable(self, registry):
        """启用时: 初始化表 + 迁移 schema + 种子检查项 + 注册定时巡检"""
        from .models import init_health_tables, migrate_alert_schema, seed_default_checks

        init_health_tables()
        migrate_alert_schema()
        seed_default_checks()
        _logger.info('Tables initialized, schema migrated, default checks seeded')

        # 初始化插件 i18n（注入 self.t 到各模块）
        from . import routes as _routes
        from . import checkers as _checkers
        from . import discovery as _discovery
        from . import scheduler_setup as _sched
        _routes.init_i18n(self.t)
        _checkers.init_i18n(self.t)
        _discovery.init_i18n(self.t)
        _sched.init_i18n(self.t)
        _logger.info('Plugin i18n initialized')

        # 注册定时巡检（写入 orchestrator cron_jobs 表）
        try:
            _sched.seed_health_schedules()
            _logger.info('Health check schedules registered')
        except Exception as e:
            _logger.warning('Schedule registration warning: %s', e)

        # 注册 Dashboard 数据注入 filter
        try:
            from plugin_manager.hooks import get_hook_registry
            _hooks = get_hook_registry()
            already = any(
                h.get('identifier') == 'health_check'
                for hooks_list in _hooks.list_filters('dashboard.data').values()
                for h in hooks_list
            )
            if not already:
                _hooks.add_filter('dashboard.data', enrich_dashboard,
                                   priority=15, identifier='health_check')
                _logger.info('Dashboard data filter registered')
        except Exception as e:
            _logger.warning('Dashboard filter registration warning: %s', e)

        return True

    def register_routes(self):
        """注册健康巡检 Blueprint"""
        from . import health_bp
        return [health_bp]

    def register_agents(self):
        """§4.1 — Register AI agents provided by this plugin."""
        return [{
            'id': 'health_ai_fixer',
            'name': 'Health AI Fixer',
            'description': 'LLM-powered health check result analysis and repair suggestion engine',
            'capabilities': ['health_analysis', 'fix_suggestion', 'root_cause_analysis'],
        }]

    def get_dashboard_stats(self) -> dict:
        """§2.3 — Return dashboard statistics for this plugin."""
        from .models import get_latest_status, get_unread_alert_count
        stats = {}
        try:
            status = get_latest_status()
            if status:
                passed = status.get('passed', 0) or 0
                warnings = status.get('warnings', 0) or 0
                errors = status.get('errors', 0) or 0
                total = passed + warnings + errors
                stats['health_score'] = round(passed * 100 / total, 1) if total > 0 else 100.0
                stats['health_passed'] = passed
                stats['health_warnings'] = warnings
                stats['health_errors'] = errors
        except Exception:
            stats['health_score'] = 100.0
        try:
            stats['unread_alerts'] = get_unread_alert_count()
        except Exception:
            stats['unread_alerts'] = 0
        return stats

    def get_schema_version(self) -> int:
        """§10.6 — Return current DB schema version for migration tracking."""
        return 2  # v1.4 schema version

    def migrate(self, from_version: int, to_version: int) -> bool:
        """§10.6 — Run schema migrations between versions."""
        from .models import migrate_alert_schema
        try:
            migrate_alert_schema()
            return True
        except Exception as e:
            _logger.error('Migration %s->%s failed: %s', from_version, to_version, e)
            return False

    def on_disable(self, registry):
        _logger.info('Disabled')
        return True

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 health schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS health CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            _logger.info('health schema dropped')
        except Exception as e:
            _logger.error('on_uninstall cleanup failed: %s', e)
        return True


# ═══════════════════════════════════════════════════════════════
# Dashboard data enrichment
# ═══════════════════════════════════════════════════════════════

def enrich_dashboard(value, conn=None):
    """从 health_check 独立 DB 注入健康数据到 Dashboard"""
    data = value
    from .models import get_latest_status, get_unread_alert_count, get_health_trend

    try:
        status = get_latest_status()
        if status:
            passed = status.get('passed', 0) or 0
            warnings = status.get('warnings', 0) or 0
            errors = status.get('errors', 0) or 0
            total = passed + warnings + errors
            # 与 health_check 插件 Overview 页保持一致: passed/total*100
            if total > 0:
                score = round(passed * 100 / total, 1)
            else:
                score = 100.0
            data['health_score'] = score
            data['health_passed'] = passed
            data['health_warnings'] = warnings
            data['health_errors'] = errors
    except Exception:
        pass
    try:
        data['unread_alerts'] = get_unread_alert_count()
    except Exception:
        pass
    try:
        trend = get_health_trend(7)
        data['health_trend_7d'] = trend if trend else []
    except Exception:
        pass

    return data