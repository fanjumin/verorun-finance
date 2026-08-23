#!/usr/bin/env python3
"""
analytics/processor.py — 异步批处理聚合引擎

职责:
  1. 从 analytics_logs 聚合数据到 hourly_stats / daily_stats / page_stats 等
  2. 管理会话超时
  3. 检查告警
  4. 清理过期原始日志
  5. 可被 APScheduler 或后台线程周期调用

聚合周期: 每 60 秒运行一次增量聚合
"""

from i18n import _
import time
import os
import sys
import logging
from datetime import datetime, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from . import models as am

logger = logging.getLogger('analytics.processor')


class AnalyticsProcessor:
    """
    分析数据处理器

    用法:
        from analytics.processor import AnalyticsProcessor
        processor = AnalyticsProcessor()
        processor.process()  # 手动触发一次聚合

    或通过 APScheduler 定时调度:
        scheduler.add_job(processor.process, 'interval', seconds=60)
    """

    def __init__(self):
        self.last_hourly_run = 0  # 上次小时聚合时间戳
        self.last_daily_run = 0   # 上次日聚合时间戳
        self.last_alert_check = 0 # 上次告警检查
        self.last_cleanup = 0     # 上次日志清理
        self.stats = {            # 当前批处理统计
            'total_batches': 0,
            'processed': {'pv': 0, 'bot': 0, 'error': 0, 'sessions': 0},
        }

    def process(self):
        """
        执行完整的处理流水线
        建议每 60 秒调用一次
        """
        conn = am.get_db()
        try:
            now = int(time.time())
            today_str = datetime.now().strftime('%Y-%m-%d')
            current_hour = datetime.now().hour

            # 1. 小时聚合（每 60 秒增量）
            if now - self.last_hourly_run >= 60:
                self._aggregate_hourly(conn, today_str, current_hour)
                self.last_hourly_run = now

            # 2. 日聚合（每 5 分钟检查一次，避免地理/来源/设备统计长时间滞后）
            if now - self.last_daily_run >= 300:
                self._aggregate_daily(conn)
                self.last_daily_run = now

            # 3. 告警检查（每 5 分钟）
            if now - self.last_alert_check >= 300:
                triggered = am.check_alerts(conn)
                if triggered:
                    self._handle_alerts(triggered)
                self.last_alert_check = now

            # 4. 日志清理（每天一次）
            if now - self.last_cleanup >= 86400:
                self._cleanup_logs(conn, today_str)
                self.last_cleanup = now

            self.stats['total_batches'] += 1

        except Exception as e:
            logger.error('Processor run failed: %s', e, exc_info=True)
        finally:
            conn.close()

        return self.stats

    def _aggregate_hourly(self, conn, today_str: str, hour: int):
        """
        从原始日志聚合到小时统计
        只处理本小时的数据（增量）
        """
        hour_start = int(datetime.now().replace(minute=0, second=0, microsecond=0).timestamp())
        hour_end = hour_start + 3600

        # 检查是否有未处理的数据
        count = conn.execute(
            "SELECT COUNT(*) c FROM analytics_logs WHERE timestamp >= ? AND timestamp < ?",
            (hour_start, hour_end)
        ).fetchone()['c']

        if count == 0:
            return

        # 各服务单独统计
        services = conn.execute(
            "SELECT DISTINCT service_name FROM analytics_logs WHERE timestamp >= ? AND timestamp < ?",
            (hour_start, hour_end)
        ).fetchall()

        for svc_row in services:
            svc = svc_row['service_name']
            self._aggregate_hourly_service(conn, today_str, hour, hour_start, hour_end, svc)

    def _aggregate_hourly_service(self, conn, today_str, hour, start, end, svc):
        """按服务聚合小时数据"""
        where = "WHERE timestamp>=? AND timestamp<? AND service_name=?"
        params = [start, end, svc]

        pv = conn.execute(
            f"SELECT COUNT(*) c FROM analytics_logs {where} AND is_bot=0",
            params
        ).fetchone()['c']

        uv = conn.execute(
            f"SELECT COUNT(DISTINCT visitor_hash) c FROM analytics_logs {where} AND is_bot=0",
            params
        ).fetchone()['c']

        ipv = conn.execute(
            f"SELECT COUNT(DISTINCT ip_prefix) c FROM analytics_logs {where} AND is_bot=0",
            params
        ).fetchone()['c']

        bot_count = conn.execute(
            f"SELECT COUNT(*) c FROM analytics_logs {where} AND is_bot=1",
            params
        ).fetchone()['c']

        error_count = conn.execute(
            f"SELECT COUNT(*) c FROM analytics_logs {where} AND is_bot=0 AND status_code>=400",
            params
        ).fetchone()['c']

        new_visitors = conn.execute(
            f"SELECT COUNT(DISTINCT visitor_hash) c FROM analytics_logs lg {where} AND is_bot=0 "
            "AND NOT EXISTS (SELECT 1 FROM analytics_logs lg2 WHERE lg2.visitor_hash=lg.visitor_hash "
            "AND lg2.timestamp<? AND lg2.is_bot=0)",
            params + [start]
        ).fetchone()['c']

        avg_response_time = conn.execute(
            f"SELECT ROUND(AVG(response_time), 0) v FROM analytics_logs {where}",
            params
        ).fetchone()['v'] or 0

        # 会话数
        session_count = conn.execute(
            "SELECT COUNT(DISTINCT session_hash) c FROM analytics_logs "
            "WHERE timestamp>=? AND timestamp<? AND service_name=? AND is_bot=0",
            (start, end, svc)
        ).fetchone()['c']

        # 跳出次数（仅 1 次请求的会话）
        bounce_count = conn.execute(
            "SELECT COUNT(*) c FROM analytics_visitor_sessions vs "
            "WHERE vs.start_time>=? AND vs.start_time<? AND vs.page_views<=1 AND vs.is_bot=0",
            (start, end)
        ).fetchone()['c']

        # 总停留时间
        total_time = conn.execute(
            "SELECT COALESCE(SUM(duration), 0) v FROM analytics_visitor_sessions "
            "WHERE start_time>=? AND start_time<? AND is_bot=0",
            (start, end)
        ).fetchone()['v']

        am.upsert_hourly(conn, today_str, hour, {
            'pv': pv, 'uv': uv, 'ipv': ipv,
            'new_visitors': new_visitors,
            'bounce_count': bounce_count,
            'total_time': total_time,
            'session_count': session_count,
            'bot_count': bot_count,
            'error_count': error_count,
            'avg_response_time': int(avg_response_time),
        }, svc)

        # 页面/来源/地理/设备统计已移至 _aggregate_daily（日级全量重算 + 覆盖语义），
        # 避免按小时重复累加导致数据虚高。

        # 更新统计数据
        self.stats['processed']['pv'] += pv
        self.stats['processed']['bot'] += bot_count
        self.stats['processed']['error'] += error_count

    def _aggregate_daily(self, conn, date_str=None):
        """从小时聚合计算每日汇总（可指定 date_str 重算历史日期；不指定则处理昨天+今天）"""
        if date_str:
            dates = [date_str]
        else:
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            today = datetime.now().strftime('%Y-%m-%d')
            dates = [yesterday, today]

        for date_str in dates:
            rows = conn.execute(
                "SELECT COALESCE(SUM(pv),0) pv, COALESCE(SUM(uv),0) uv, "
                "COALESCE(SUM(ipv),0) ipv, COALESCE(SUM(new_visitors),0) nv, "
                "COALESCE(SUM(session_count),0) sessions, "
                "COALESCE(SUM(bot_count),0) bots, "
                "COALESCE(SUM(error_count),0) errors, "
                "COALESCE(SUM(total_time),0) total_time, "
                "COALESCE(SUM(bounce_count),0) bounce_count, "
                "COALESCE(AVG(avg_response_time),0) avg_resp "
                "FROM analytics_hourly_stats WHERE date=?",
                (date_str,)
            ).fetchone()

            if rows['pv'] == 0:
                continue

            # 日 UV（独立访客）
            day_start = int(datetime.strptime(date_str, '%Y-%m-%d').timestamp())
            day_end = day_start + 86400
            daily_uv = conn.execute(
                "SELECT COUNT(DISTINCT visitor_hash) c FROM analytics_logs "
                "WHERE timestamp>=? AND timestamp<? AND is_bot=0",
                (day_start, day_end)
            ).fetchone()['c']

            daily_ipv = conn.execute(
                "SELECT COUNT(DISTINCT ip_prefix) c FROM analytics_logs "
                "WHERE timestamp>=? AND timestamp<? AND is_bot=0",
                (day_start, day_end)
            ).fetchone()['c']

            sessions = int(rows['sessions'])
            bounce_rate = 0.0
            avg_duration = 0.0
            avg_depth = 0.0
            returning_visitors = 0

            if sessions > 0:
                bounce_rate = round(float(rows['bounce_count']) * 100.0 / sessions, 2)
                avg_duration = round(float(rows['total_time']) / sessions, 1)
                avg_depth = round(float(rows['pv']) / sessions, 1)

            # 回访用户
            if daily_uv > 0:
                returning_visitors = daily_uv - rows['nv']
                if returning_visitors < 0:
                    returning_visitors = 0

            # 最高同时在线（5分钟窗口）— 单条窗口查询，替代 288 次循环
            peak_concurrent = 0
            peak_time = ''
            try:
                row = conn.execute(
                    "SELECT gs.ws AS window_start, COUNT(DISTINCT lg.visitor_hash) AS cnt "
                    "FROM generate_series(%s, %s, 300) gs(ws) "
                    "LEFT JOIN analytics_logs lg "
                    "  ON lg.timestamp >= gs.ws AND lg.timestamp < gs.ws + 300 AND lg.is_bot = 0 "
                    "GROUP BY gs.ws ORDER BY cnt DESC, gs.ws ASC LIMIT 1",
                    (day_start, day_end - 1)
                ).fetchone()
                if row and row['cnt']:
                    peak_concurrent = int(row['cnt'])
                    peak_time = datetime.fromtimestamp(int(row['window_start'])).strftime('%H:%M')
            except Exception as e:
                logger.warning('Peak concurrent query failed: %s', e, exc_info=True)

            am.upsert_daily(conn, date_str, {
                'pv': rows['pv'], 'uv': daily_uv, 'ipv': daily_ipv,
                'new_visitors': rows['nv'],
                'returning_visitors': returning_visitors,
                'bounce_rate': bounce_rate,
                'avg_session_duration': avg_duration,
                'avg_depth': avg_depth,
                'bot_pv': rows['bots'],
                'error_pv': rows['errors'],
                'avg_response_time': int(rows['avg_resp']),
                'total_sessions': sessions,
            })

            # 日级 页面/来源/地理/设备 统计（全量重算当日 + 覆盖语义）
            day_where = "WHERE timestamp>=? AND timestamp<? AND is_bot=0"
            day_params = [day_start, day_end]

            pages = conn.execute(
                "SELECT path, COUNT(*) pv, COUNT(DISTINCT visitor_hash) uv, "
                "ROUND(AVG(response_time), 0) avg_time "
                f"FROM analytics_logs {day_where} "
                "GROUP BY path",
                day_params
            ).fetchall()

            for pg in pages:
                entries = conn.execute(
                    "SELECT COUNT(*) c FROM analytics_visitor_sessions "
                    "WHERE entry_path=? AND start_time>=? AND start_time<? AND is_bot=0",
                    (pg['path'], day_start, day_end)
                ).fetchone()['c']

                exits = conn.execute(
                    "SELECT COUNT(*) c FROM analytics_visitor_sessions "
                    "WHERE exit_path=? AND start_time>=? AND start_time<? AND is_bot=0",
                    (pg['path'], day_start, day_end)
                ).fetchone()['c']

                am.upsert_page_stat(conn, date_str, pg['path'], {
                    'pv': pg['pv'], 'uv': pg['uv'],
                    'entries': entries, 'exits': exits,
                    'total_time': pg['pv'] * pg['avg_time'] if pg['avg_time'] else 0,
                })

            sources = conn.execute(
                "SELECT referer_domain, MIN(referer) referer, COUNT(*) pv, "
                "COUNT(DISTINCT visitor_hash) uv "
                f"FROM analytics_logs {day_where} AND referer!='' "
                "GROUP BY referer_domain",
                day_params
            ).fetchall()

            for src in sources:
                s_type, s_name = am.classify_source(src['referer'])
                am.upsert_source(conn, date_str, s_type, s_name or 'direct', {
                    'pv': src['pv'], 'uv': src['uv'],
                })

            geos = conn.execute(
                "SELECT country, city, COUNT(*) pv, COUNT(DISTINCT visitor_hash) uv "
                f"FROM analytics_logs {day_where} AND country!='' "
                "GROUP BY country, city",
                day_params
            ).fetchall()

            for g in geos:
                am.upsert_geo(conn, date_str, g['country'], g['city'], {
                    'pv': g['pv'], 'uv': g['uv'],
                })

            devices = conn.execute(
                "SELECT device_type, browser, os_name, COUNT(*) pv, "
                "COUNT(DISTINCT visitor_hash) uv "
                f"FROM analytics_logs {day_where} "
                "GROUP BY device_type, browser, os_name",
                day_params
            ).fetchall()

            for d in devices:
                am.upsert_device(conn, date_str, d['device_type'], d['browser'], d['os_name'], {
                    'pv': d['pv'], 'uv': d['uv'],
                })

        logger.info('Daily aggregation completed [%s]', ', '.join(dates))

    @staticmethod
    def _get_admin_ids(raw):
        """查询所有启用的管理员用户（users 表: is_admin=1 AND active=1），
        查不到时回退到 user_id=1，避免告警无人接收。"""
        ids = []
        try:
            cur = raw.cursor()
            cur.execute("SELECT id FROM users WHERE is_admin=1 AND active=1")
            ids = [row[0] for row in cur.fetchall()]
            cur.close()
        except Exception as e:
            logger.warning('Failed to query admin users: %s', e, exc_info=True)
        return ids or [1]

    def _handle_alerts(self, triggered: list):
        """处理触发的告警"""
        for alert in triggered:
            msg = _("🚨 Alert triggered: {name}\n"
                    "Metric: {metric}\n"
                    "Current value: {value} (threshold: {op} {threshold})\n"
                    "Time Window: {window}").format(
                        name=alert["name"],
                        metric=alert["metric"],
                        value=alert["current_value"],
                        op=alert["operator"],
                        threshold=alert["threshold"],
                        window=alert["time_window"])
            logger.info('Alert: %s', msg)

            # 写入通知（集成到现有通知系统，发往所有启用状态的管理员）
            try:
                from plugins._base.db import get_raw_connection
                raw = get_raw_connection()
                raw.autocommit = True
                title = _("Statistical analysis alert: {name}").format(name=alert["name"])
                cur = raw.cursor()
                for uid in self._get_admin_ids(raw):
                    cur.execute(
                        "INSERT INTO notifications (user_id, title, content, type, created_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (uid, title, msg, 'alert', int(time.time()))
                    )
                cur.close()
            except Exception as e:
                logger.warning('Notification write failed: %s', e, exc_info=True)

    def _cleanup_logs(self, conn, today_str: str):
        """清理过期日志"""
        config = am.get_privacy_config(conn)
        retention = int(config.get('log_retention_days', 30))
        deleted = am.cleanup_old_logs(conn, retention)
        logger.info('Cleaned %s expired raw logs (kept %s days)', deleted, retention)


# ─── 快捷函数 ──────────────────────────────────────────────────────────────────

def run_once():
    """运行一次完整的处理流水线"""
    processor = AnalyticsProcessor()
    return processor.process()


def run_forever(interval: int = 60):
    """
    以指定间隔持续运行
    适合在后台线程或独立进程中运行
    """
    import signal

    processor = AnalyticsProcessor()
    logger.info('Started aggregation loop (interval %ss)', interval)
    logger.info('Press Ctrl+C to stop')

    running = True

    def _signal_handler(sig, frame):
        nonlocal running
        logger.info('Stopping...')
        running = False

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, 'SIGTERM'):  # Windows 平台无 SIGTERM 信号
        signal.signal(signal.SIGTERM, _signal_handler)

    while running:
        try:
            stats = processor.process()
            time.sleep(interval)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.warning('%s', e, exc_info=True)
            time.sleep(interval)

    logger.info('Stopped')


if __name__ == '__main__':
    run_forever(interval=60)
