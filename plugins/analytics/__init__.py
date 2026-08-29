#!/usr/bin/env python3
"""
Analytics Plugin — Server-side Cookieless Analytics
=====================================================
独立数据库 data/analytics.db，不依赖主库。

插件能力:
  - on_enable:  初始化 11 张分析表 + 注册请求中间件 + 启动后台聚合线程
  - on_disable: 卸载 Blueprint（中间件需重启才生效）
  - register_routes: 注册 /admin/analytics 仪表盘
"""

import os
import sys
import threading
import logging

# 确保 analytics 包可导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin
from plugin_manager.hooks import get_hook_registry

logger = logging.getLogger('analytics')


class AnalyticsPlugin(BasePlugin):
    name = 'analytics'
    identifier = 'analytics'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Analytics Middleware & Dashboard — Server-side Cookieless Analytics'
    author = 'VeroRun'

    _middleware = None
    _processor_thread = None

    def get_config_value(self, key: str, default=None):
        """优先 PluginManager，回退到 plugin.json 默认值"""
        try:
            mgr = getattr(self.app.extensions, 'get', lambda x: None)('plugin_manager')
            if mgr:
                pm_cfg = mgr.get_config(self.identifier) or {}
                if key in pm_cfg:
                    return pm_cfg[key]
        except Exception:
            logger.warning('Failed to read plugin config key %r', key, exc_info=True)
        return self._config.get(key, default)

    def on_install(self, registry):
        """安装时初始化独立数据库表"""
        from .models import init_analytics_tables
        try:
            init_analytics_tables()
            logger.info('Independent DB initialized (data/analytics.db)')
        except Exception as e:
            logger.warning('DB init warning: %s', e, exc_info=True)
        return True

    def on_enable(self, registry):
        """启用时: 注册中间件 + 启动聚合线程 + 初始化 i18n"""
        from .middleware import AnalyticsMiddleware
        from .processor import AnalyticsProcessor
        from .routes import init_i18n

        # 初始化 i18n
        init_i18n(self.t)

        # 初始化数据库表（幂等）
        from .models import init_analytics_tables
        init_analytics_tables()

        # 注册请求中间件
        sample_rate = self.get_config_value('sample_rate', 1.0)
        geoip_enabled = self.get_config_value('geoip_enabled', True)
        service_name = self.get_config_value('service_name', 'admin')

        self._middleware = AnalyticsMiddleware(
            self.app,
            service_name=service_name,
            geoip_enabled=geoip_enabled,
            sample_rate=sample_rate
        )

        # 启动后台聚合处理器（每 60 秒）
        processor = AnalyticsProcessor()
        self._processor_stop = threading.Event()

        def _loop():
            import time
            while not self._processor_stop.is_set():
                try:
                    processor.process()
                except Exception as e:
                    logger.warning('Processor background run error: %s', e, exc_info=True)
                time.sleep(60)

        self._processor_thread = threading.Thread(
            target=_loop, daemon=True, name='analytics-processor'
        )
        self._processor_thread.start()

        logger.info('Middleware registered [%s] sample_rate=%s', service_name, sample_rate)
        logger.info('Background processor started (60s interval)')
        logger.info('Dashboard filter registered (module-level)')
        return True

    def register_routes(self):
        """注册 Analytics 仪表盘 Blueprint"""
        from .routes import analytics_bp
        return [analytics_bp]

    def on_disable(self, registry):
        """禁用时: 停止后台线程 + 卸载 Blueprint
        注意: Flask 中间件无法热卸载，需重启服务后完全移除
        """
        # 优雅停止后台聚合线程
        if getattr(self, '_processor_stop', None) is not None:
            self._processor_stop.set()
        if getattr(self, '_processor_thread', None) is not None:
            self._processor_thread.join(timeout=10)
            self._processor_thread = None
        self._middleware = None
        logger.info('Disabled — restart required to fully remove middleware')
        return True

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读 analytics 独立 PG，幂等）。

        指标: 今日 PV/UV、实时在线、30 日总 PV。
        """
        from datetime import datetime, timedelta
        stats = {'today_pv': 0, 'today_uv': 0, 'online_now': 0, 'total_pv_30d': 0}
        try:
            from .models import get_db
            conn = get_db()
            try:
                today_str = datetime.now().strftime('%Y-%m-%d')
                since_str = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
                row = conn.execute(
                    'SELECT pv, uv FROM analytics_daily_stats WHERE date=%s', (today_str,)
                ).fetchone()
                if row:
                    stats['today_pv'] = int(row['pv'] or 0)
                    stats['today_uv'] = int(row['uv'] or 0)
                online = conn.execute(
                    'SELECT COUNT(DISTINCT visitor_hash) AS c FROM analytics_visitor_sessions '
                    'WHERE end_time>=EXTRACT(EPOCH FROM NOW())-300'
                ).fetchone()
                stats['online_now'] = int(online['c']) if online else 0
                total = conn.execute(
                    'SELECT COALESCE(SUM(pv),0) AS c FROM analytics_daily_stats WHERE date>=%s',
                    (since_str,)
                ).fetchone()
                stats['total_pv_30d'] = int(total['c']) if total else 0
            finally:
                conn.close()
        except Exception:
            logger.warning('get_dashboard_stats failed', exc_info=True)
        return stats

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 analytics schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS analytics CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('analytics schema dropped')
        except Exception as e:
            logger.error('on_uninstall cleanup failed: %s', e, exc_info=True)
        return True


# ═══════════════════════════════════════════════════════════════
# Module-level: dashboard.data filter + enrich function
# 在模块导入时注册，不依赖 on_enable() 调用。
# 当 PluginManager 跳过 enable()（插件已 ACTIVE）时仍能工作。
# ═══════════════════════════════════════════════════════════════

def enrich_dashboard(value, conn=None):
    """从 analytics 独立 PG 查询仪表盘数据，注入到 data dict

    双库策略:
      - analytics 特化表（daily_stats / visitor_sessions / page_stats）
        → 通过 .models.get_db() 连接独立 PostgreSQL
      - 主库表（agent_token_daily / billing_orders）
        → 复用上层传入的 conn（SQLite / PG 均可）
    """
    from datetime import datetime, timedelta
    data = value
    today_str = datetime.now().strftime('%Y-%m-%d')          # 避免 CURRENT_DATE 隐式类型转换
    since_30d_str = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    # ── Part A: Analytics PG 连接 ──
    try:
        from .models import get_db as get_analytics_db
        aconn = get_analytics_db()
    except Exception:
        logger.warning('Failed to open analytics DB for dashboard enrich', exc_info=True)
        aconn = None

    if aconn is not None:
        def _rb():
            try:
                aconn.rollback()
            except Exception:
                logger.warning('Analytics dashboard rollback failed', exc_info=True)
        try:
            pvuv = aconn.execute(
                "SELECT pv, uv FROM analytics_daily_stats WHERE date=%s",
                (today_str,)
            ).fetchone()
            data['today_pv'] = pvuv['pv'] if pvuv else 0
            data['today_uv'] = pvuv['uv'] if pvuv else 0
        except Exception:
            logger.warning('Failed to query today PV/UV for dashboard', exc_info=True)
        finally:
            _rb()
        try:
            online = aconn.execute(
                "SELECT COUNT(DISTINCT visitor_hash) as c FROM analytics_visitor_sessions "
                "WHERE end_time>=EXTRACT(EPOCH FROM NOW()) - 300"
            ).fetchone()
            data['online_now'] = online['c'] if online else 0
        except Exception:
            logger.warning('Failed to query online visitors for dashboard', exc_info=True)
        finally:
            _rb()
        try:
            pages = aconn.execute(
                "SELECT path, pv FROM analytics_page_stats WHERE date=%s "
                "ORDER BY pv DESC LIMIT 3",
                (today_str,)
            ).fetchall()
            data['top_pages'] = [{'path': r['path'], 'pv': r['pv']} for r in pages]
        except Exception:
            logger.warning('Failed to query top pages for dashboard', exc_info=True)
        finally:
            _rb()
        try:
            trend = aconn.execute(
                "SELECT date, pv, uv FROM analytics_daily_stats "
                "WHERE date >= %s ORDER BY date ASC",
                (since_30d_str,)
            ).fetchall()
            data['trend_30d'] = [dict(r) for r in trend]
        except Exception:
            logger.warning('Failed to query 30d trend for dashboard', exc_info=True)
        finally:
            aconn.close()

    # ── Part B: 主库连接（conn 由上层 dashboard() 传入） ──
    if conn is not None:
        def _rb_conn():
            try:
                conn.rollback()
            except Exception:
                logger.warning('Main dashboard rollback failed', exc_info=True)
        try:
            r = conn.execute(
                "SELECT COALESCE(SUM(total_tokens),0) as c FROM agent_token_daily WHERE stat_date=%s",
                (today_str,)
            ).fetchone()
            data['today_tokens'] = r['c'] if r else 0
        except Exception:
            logger.warning('Failed to query today tokens for dashboard', exc_info=True)
        finally:
            _rb_conn()
        try:
            agents = conn.execute(
                "SELECT t.agent_id, t.agent_name, t.total_tokens as total "
                "FROM agent_token_daily t WHERE t.stat_date=%s "
                "ORDER BY t.total_tokens DESC LIMIT 3",
                (today_str,)
            ).fetchall()
            data['top_token_agents'] = [dict(r) for r in agents]
        except Exception:
            logger.warning('Failed to query top token agents for dashboard', exc_info=True)
        finally:
            _rb_conn()
        try:
            rev = conn.execute(
                "SELECT DATE(paid_at) as date, COALESCE(SUM(amount),0) as revenue "
                "FROM billing_orders WHERE status='paid' AND paid_at >= %s "
                "GROUP BY DATE(paid_at) ORDER BY date ASC",
                (since_30d_str,)
            ).fetchall()
            data['revenue_trend_30d'] = [dict(r) for r in rev]
        except Exception:
            logger.warning('Failed to query revenue trend for dashboard', exc_info=True)
        finally:
            _rb_conn()
    return data


# 注册 filter（模块导入时执行，worker 重启后自动生效）
_hooks = get_hook_registry()
existing = _hooks.list_filters('dashboard.data')
already = any(
    h.get('identifier') == 'analytics'
    for hooks_list in existing.values()
    for h in hooks_list
)
if not already:
    _hooks.add_filter('dashboard.data', enrich_dashboard,
                       priority=10, identifier='analytics')
    logger.info('Module-level dashboard filter registered')
else:
    logger.info('Dashboard filter already registered, skip')
