#!/usr/bin/env python3
"""
analytics/cli.py — 分析系统 CLI 维护工具

用法:
  python3 -m analytics.cli init                  # 初始化分析数据库表
  python3 -m analytics.cli process                # 运行一次聚合处理
  python3 -m analytics.cli daemon                 # 启动聚合守护进程
  python3 -m analytics.cli report [days=7]        # 生成文本报告
  python3 -m analytics.cli cleanup [days=30]      # 清理过期日志
  python3 -m analytics.cli stats                  # 分析系统自身统计
  python3 -m analytics.cli add-alert              # 交互式添加告警
  python3 -m analytics.cli seed-workflows         # 创建预设工作流
"""

from i18n import _
import os
import sys
import json
import time
import signal
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from . import models as am
from .tracker import generate_report, generate_insight_text, create_alert
from .processor import AnalyticsProcessor


def cmd_init():
    """初始化分析数据库表"""
    am.init_analytics_tables()
    conn = am.get_db()
    try:
        config = am.get_privacy_config(conn)
        print(f'[Analytics] 📋 Privacy Settings:')
        for k, v in config.items():
            print(f'  {k}: {v}')
    finally:
        conn.close()


def cmd_process():
    """运行一次聚合处理"""
    processor = AnalyticsProcessor()
    stats = processor.process()
    print(f'[Analytics] ✅ Aggregation completed')
    print(f'  Processed Batches: {stats["total_batches"]}')
    print(f'  PV: {stats["processed"]["pv"]}')
    print(f'  Bot: {stats["processed"]["bot"]}')
    print(_('  Errors: {n}').format(n=stats["processed"]["error"]))


def cmd_daemon():
    """启动聚合守护进程"""
    interval = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    processor = AnalyticsProcessor()
    running = True

    def handler(sig, frame):
        nonlocal running
        print(_('\n[Analytics Daemon] Stopping...'))
        running = False

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, 'SIGTERM'):  # Windows 平台无 SIGTERM 信号
        signal.signal(signal.SIGTERM, handler)

    print(f'[Analytics Daemon] 🚀 Started (interval {interval}s)')
    while running:
        try:
            processor.process()
            time.sleep(interval)
        except Exception as e:
            print(f'[Analytics Daemon] ⚠️ {e}')
            time.sleep(interval)

    print(_('[Analytics Daemon] ✅ Stopped'))


def cmd_report():
    """生成报告"""
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    report = generate_report(days)
    text = generate_insight_text(report)
    print(text)


def cmd_cleanup():
    """清理过期日志"""
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    conn = am.get_db()
    try:
        deleted = am.cleanup_old_logs(conn, days)
        print(f'[Analytics] 🧹 Cleaned {deleted} logs (kept {days} days)')
    finally:
        conn.close()


def cmd_stats():
    """分析系统自身统计"""
    conn = am.get_db()
    try:
        sys_info = am.get_privacy_config(conn) if hasattr(am, 'get_privacy_config') else {}

        total_logs = conn.execute("SELECT COUNT(*) c FROM analytics_logs").fetchone()['c']
        total_hourly = conn.execute("SELECT COUNT(*) c FROM analytics_hourly_stats").fetchone()['c']
        total_daily = conn.execute("SELECT COUNT(*) c FROM analytics_daily_stats").fetchone()['c']
        total_events = conn.execute("SELECT COUNT(*) c FROM analytics_events").fetchone()['c']
        total_sessions = conn.execute("SELECT COUNT(*) c FROM analytics_visitor_sessions").fetchone()['c']

        today = datetime.now().strftime('%Y-%m-%d')
        today_pv = conn.execute(
            "SELECT COUNT(*) c FROM analytics_logs WHERE to_timestamp(timestamp)::date=%s AND is_bot=0",
            (today,)
        ).fetchone()['c']
        today_uv = conn.execute(
            "SELECT COUNT(DISTINCT visitor_hash) c FROM analytics_logs WHERE to_timestamp(timestamp)::date=%s AND is_bot=0",
            (today,)
        ).fetchone()['c']

        # 数据库大小
        db_size = conn.execute(
            "SELECT pg_database_size(current_database()) as size"
        ).fetchone()['size']

        print('═' * 45)
        print(_('  📊 Analytics System Status'))
        print('═' * 45)
        print(f'  Raw Logs:     {total_logs:,} items')
        print(f'  Hourly Aggregation:     {total_hourly:,} items')
        print(f'  Daily Aggregation:       {total_daily:,} items')
        print(f'  Events:         {total_events:,} items')
        print(f'  Sessions:         {total_sessions:,} items')
        print(f'  Today PV:      {today_pv:,}')
        print(f'  Today UV:      {today_uv:,}')
        print(f'  Database Size:   {db_size/1048576:.2f} MB')
        print('─' * 45)

    except Exception as e:
        print(f'[Analytics] ❌ Query failed: {e}')
    finally:
        conn.close()


def cmd_add_alert():
    """交互式添加告警规则"""
    import readline  # 增强输入体验
    print(_('\n=== Add Analytics Alert Rule ==='))
    print()

    name = input(_('Alert name: ')).strip()
    if not name:
        print(_('❌ Name Cannot Be Empty'))
        return

    print(_('\nMetric options:'))
    metrics = [_('UV (Unique Visitors)'), _('PV (Page Views)'), _('bounce_rate (Bounce rate %)'),
               _('Error rate (%)'), _('avg_response_time (Average response ms)')]
    for i, m in enumerate(metrics, 1):
        print(f'  {i}. {m}')
    metric_idx = int(input(_('Select metric (1-5): ')).strip())
    metric_map = ['', 'uv', 'pv', 'bounce_rate', 'error_rate', 'avg_response_time']
    metric = metric_map[metric_idx]

    print(_('\nOperators:'))
    print(_('  1. > (Greater than)'))
    print(_('  2. < (Less than)'))
    print(_('  3. >= (Greater than or equal to)'))
    print(_('  4. <= (Less than or equal to)'))
    op_idx = int(input(_('Select operator (1-4): ')).strip())
    op_map = ['', 'gt', 'lt', 'gte', 'lte']
    operator = op_map[op_idx]

    threshold = float(input(_('Threshold: ')).strip())

    print(_('\nTime window:'))
    print(_('  1. 1h (1 hour)'))
    print(_('  2. 24h (24 hours)'))
    print(_('  3. 7d (7 days)'))
    tw_idx = int(input(_('Select (1-3): ')).strip())
    tw_map = ['', '1h', '24h', '7d']
    time_window = tw_map[tw_idx]

    alert_id = create_alert(name, metric, operator, threshold, time_window)
    print(_('\n✅ Alert rule created (ID: {})').format(alert_id))


def cmd_seed_workflows():
    """创建预设分析工作流"""
    try:
        from .workflow_nodes import create_daily_report_workflow, create_weekly_report_workflow
        from orchestrator import models as om
        om.init_orchestrator_tables()
    except ImportError:
        print(_('❌ Orchestrator Module Required (pip install apscheduler)'))
        return

    conn = om.get_db()
    try:
        daily_id = create_daily_report_workflow(conn)
        weekly_id = create_weekly_report_workflow(conn)
        print(_('✅ Predefined Workflow Created:'))
        print(_('  📊 Daily Analysis Report (ID: {})').format(daily_id))
        print(_('  📊 Weekly Operations Report (ID: {})').format(weekly_id))

        # 创建工作流绑定的 Cron 任务（cron 表达式可用环境变量覆盖，默认每日 8:00 / 每周一 9:00）
        daily_cron = os.environ.get('ANALYTICS_CRON_DAILY', '0 8 * * *')
        weekly_cron = os.environ.get('ANALYTICS_CRON_WEEKLY', '0 9 * * 1')
        from orchestrator import models as om
        om.create_cron_job(conn, {
            'name': _('Daily Analysis Report'),
            'target_type': 'workflow',
            'target_id': daily_id,
            'cron_expression': daily_cron,
            'is_active': 1,
            'priority': 3,
            'timeout': 300,
        })
        om.create_cron_job(conn, {
            'name': _('Weekly Operations Report'),
            'target_type': 'workflow',
            'target_id': weekly_id,
            'cron_expression': weekly_cron,
            'is_active': 1,
            'priority': 3,
            'timeout': 300,
        })
        print(_('  ⏰ Cron Job Bound (Daily 8:00 / Weekly Monday 9:00)'))
    finally:
        conn.close()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    commands = {
        'init': cmd_init,
        'process': cmd_process,
        'daemon': cmd_daemon,
        'report': cmd_report,
        'cleanup': cmd_cleanup,
        'stats': cmd_stats,
        'add-alert': cmd_add_alert,
        'seed-workflows': cmd_seed_workflows,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f'❌ Unknown Command: {cmd}')
        print(__doc__)
