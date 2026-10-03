#!/usr/bin/env python3
"""
Scheduler — Cron 任务调度器核心
==============================
基于 APScheduler 的扩展调度器，支持：
- 标准 Cron 表达式 + 自定义工作日历
- 一次性/重复/固定间隔任务
- 优先级调度（critical/high/normal/low）
- 失败重试（指数退避）
- 超时控制和强制终止
- 两种 Agent 类型分发（system/user）
- 分布式支持（基于数据库锁）
- SQLite Job Store（系统重启不丢失）

@package orchestrator
"""

import os, sys, time, json
import platform
import threading
from shared.logging import get_logger

logger = get_logger(__name__)
from datetime import datetime, timedelta
from i18n import _
from typing import Optional, Callable
from functools import wraps

# 添加项目根到路径（兼容独立进程和嵌入运行）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, '..', 'auth-center'))

try:
    import apscheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False

from . import models as m


# ============================================================
# 常量
# ============================================================

PRIORITY_ORDER = {'critical': 0, 'high': 1, 'normal': 2, 'low': 3}

# ============================================================
# 自然语言 Cron 解析
# ============================================================

NATURAL_RULES = {
    # 交易日 — 周一至周五
    _('Each trading day'): '0 30 9 ? * MON-FRI',
    _('Trading day opens'): '0 30 9 ? * MON-FRI',
    _('Trading day closes'): '0 0 15 ? * MON-FRI',
    '交易时段': '*/30 9-15 ? * MON-FRI',
    # 固定时间
    _('Hourly'): '0 0 * * *',
    '每半小时': '*/30 * * * *',
    '每10分钟': '*/10 * * * *',
    _('8 AM'): '0 0 8 * * *',
    _('9 AM'): '0 0 9 * * *',
    _('Midday'): '0 0 12 * * *',
    _('8 PM'): '0 0 20 * * *',
    _('Midnight'): '0 0 0 * * *',
    _('Daily'): '0 0 0 * * *',
    _('Daily'): '0 0 0 * * *',
    _('Every Monday'): '0 0 0 * * 1',
    _('First of each month'): '0 0 0 1 * *',
}

def parse_natural_cron(expr: str) -> str:
    """将自然语言表达式转为 Cron 表达式"""
    if not expr:
        return ''

    # 精确匹配
    if expr in NATURAL_RULES:
        return NATURAL_RULES[expr]

    # 尝试模式匹配
    import re
    # "每个交易日 HH:MM 执行"
    trade_match = re.match(r'每个交易日\s*(\d{1,2}):(\d{2})', expr)
    if trade_match:
        h, m = trade_match.group(1), trade_match.group(2)
        return f'0 {m} {h} ? * MON-FRI'

    # "每 X 分钟/小时"
    interval_match = re.match(r'每\s*(\d+)\s*(分钟|小时)', expr)
    if interval_match:
        num = int(interval_match.group(1))
        unit = interval_match.group(2)
        if unit == _('Minute'):
            return f'*/{num} * * * *'
        else:
            return f'0 */{num} * * *'

    # "每天 HH:MM"
    daily_match = re.match(r'每天\s*(\d{1,2}):(\d{2})', expr)
    if daily_match:
        h, m = daily_match.group(1), daily_match.group(2)
        return f'0 {m} {h} * * *'

    # 提取 hh:mm 模式
    time_match = re.search(r'(\d{1,2}):(\d{2})', expr)
    if time_match:
        h, m = time_match.group(1), time_match.group(2)
        # 检查是否包含 周/日 信息
        if _('Week') in expr or _('Week') in expr:
            day_map = {_('One'): 1, _('Two'): 2, _('Three'): 3, _('Four'): 4, _('Five'): 5, _('Saturday'): 6, _('Day'): 7, _('Day"'): 7}
            for cn, num in day_map.items():
                if cn in expr:
                    return f'0 {m} {h} * * {num}'
            return f'0 {m} {h} * * *'
        return f'0 {m} {h} * * *'

    return expr  # 兜底：当作标准 cron 表达式


# ============================================================
# 调度器核心
# ============================================================

class SchedulerEngine:
    """Cron 任务调度器引擎"""

    # ── A6：真选主（行级 CAS 租约） ──
    # 为何不用 pg_try_advisory_lock：本仓 m.get_db() 是**短连接上下文管理器**
    # （每次 psycopg2.connect，finally 关闭），会话级 advisory lock 会随连接关闭
    # 立即释放，无法承担"持锁"职责。故改用 scheduler_leader 表的行级 CAS 抢主。
    SCHEDULER_LEADER_NAME = 'automation'
    SCHEDULER_LEADER_TTL_SECONDS = 90        # 租约 TTL；心跳 30s 续租，留 3 次容错
    SCHEDULER_LEADER_ELECTION_KEY = 'scheduler_leader_election'   # 缝位④ 回退开关
    HEARTBEAT_JOB_ID = '_scheduler_heartbeat'

    def __init__(self, scheduler_id: str = None, db_url: str = None):
        hostname = platform.node() or os.environ.get('COMPUTERNAME', 'localhost')
        self.scheduler_id = scheduler_id or f'scheduler-{hostname}-{os.getpid()}'
        self._lock = threading.Lock()
        self._running_jobs: dict = {}  # job_id -> APScheduler job
        self._workflow_runner: Optional[Callable] = None
        self._callback_map: dict = {}  # target_type -> handler function
        self._is_leader = True             # 首次定主前按旧行为运行；start() 会立即定主
        self._leadership_checked = False   # 是否已定过主（避免重复挂起/恢复）
        # 插件定时作业归属表：插件 job_id -> plugin identifier。
        # 卸载插件时据此刻度摘除其全部 cron，避免存活作业占用插件 schema
        # 导致 DROP SCHEMA CASCADE 失败（memory_engine 卸载残留 9 表的诱因之一）。
        self._plugin_jobs: dict = {}

        if not HAS_APSCHEDULER:
            raise ImportError(
                "APScheduler 未安装。请运行: pip install apscheduler sqlalchemy"
            )

        # APScheduler 配置
        jobstores = {
            'default': MemoryJobStore()
        }
        # 执行器只保留默认线程池：'processpool' 从未被任何 add_job(executor=...)
        # 使用，但 APScheduler 在 start() 时会实例化它 → 每个 worker 常驻多出 2 个
        # 子进程，并因多进程资源追踪器/信号量在频繁 worker 重启下刷
        # multiprocessing finalizer sem_unlink Traceback（1.6G 内存服务器上无收益）。
        executors = {
            'default': ThreadPoolExecutor(8),
        }

        self._apscheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults={
                'coalesce': True,           # 合并错过的执行
                'max_instances': 1,          # 防止并发
                'misfire_grace_time': 300,   # 5分钟容错
            },
            timezone='Asia/Shanghai'
        )

        # 注册事件监听
        self._apscheduler.add_listener(
            self._on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
        )

    # ---- 注册处理器 ----

    def register_callback(self, target_type: str, handler: Callable):
        """注册特定 target_type 的处理函数"""
        self._callback_map[target_type] = handler

    def set_workflow_runner(self, runner: Callable):
        """设置工作流执行器"""
        self._workflow_runner = runner

    def add_plugin_job(self, job: dict, plugin_id: str = None) -> bool:
        """注册一个插件定时任务（消费插件 register_jobs() 的 APScheduler job dict）。

        兼容 dict 格式: {id, func, trigger: 'cron'|'interval'|'date', hour, minute, ...}。
        进程内注册到 APScheduler，不写入 cron_jobs 表；服务重启后由
        init_automation() 重新扫描注册，天然幂等。

        plugin_id 非空时登记作业归属，供卸载时 remove_plugin_jobs 整插件摘除。
        """
        job_id = job.get('id')
        func = job.get('func')
        if not job_id or not callable(func):
            return False
        kwargs = {k: v for k, v in job.items()
                  if k not in ('id', 'func', 'name')}
        try:
            self._apscheduler.add_job(
                func,
                id=f'plugin_{job_id}',
                name=job.get('name', job_id),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                replace_existing=True,
                **kwargs,
            )
            m.add_log('system', 0, 'info',
                       f'📅 Plugin job scheduled: [{job_id}]')
            if plugin_id:
                self._plugin_jobs[job_id] = plugin_id
            # A6：非 leader 副本注册的插件作业同样立即挂起
            if self._leadership_checked and not self._is_leader:
                try:
                    self._apscheduler.pause_job(f'plugin_{job_id}')
                except Exception as e:
                    logger.warning(f'[Scheduler] pause plugin_{job_id} on non-leader failed: {e}')
            return True
        except Exception as e:
            m.add_log('system', 0, 'error',
                      f'Plugin job [{job_id}] schedule failed: {e}')
            return False

    def remove_plugin_jobs(self, plugin_id: str) -> int:
        """卸载插件前摘除其登记的全部 APScheduler 作业。

        存活的 cron（及其持有连接/线程）会占用插件 schema，是 on_uninstall 中
        DROP SCHEMA CASCADE 失败、数据表残留的直接诱因之一。作业为进程内注册、
        重启后按 ACTIVE 插件重建，故此处只做内存摘除，不动任何持久状态。
        返回实际摘除（含本就不存在）的作业数。
        """
        removed = 0
        for job_id, owner in list(self._plugin_jobs.items()):
            if owner != plugin_id:
                continue
            ap_id = f'plugin_{job_id}'
            try:
                self._apscheduler.remove_job(ap_id)
            except Exception as e:
                # JobLookupError（作业本就不存在/已摘）属预期；其余仅告警，
                # 不阻断卸载主流程（DROP 仍会尝试，失败由 on_uninstall 结果上抛）。
                logger.warning(f'[Scheduler] remove plugin job {ap_id} warning: {e}')
            finally:
                self._plugin_jobs.pop(job_id, None)
                removed += 1
        return removed

    # ---- 生命周期 ----

    def start(self):
        """启动调度器"""
        m.add_log('system', 0, 'info',
                   f'🟢 Scheduler started: {self.scheduler_id}',
                   {'scheduler_id': self.scheduler_id})
        self._apscheduler.start()
        self._register_scheduler_heartbeat()
        self._sync_cron_jobs()
        # A6：启动即定主。否则首个心跳周期（30s）内所有副本都处于"未定主"状态，
        # 多副本会在这个窗口里重复触发同一 cron —— 正是 A6 要消除的重复执行。
        try:
            self._apply_leadership(self._try_acquire_leadership())
        except Exception as e:
            logger.error(f'[Scheduler] initial leadership probe failed: {e}')
            self._apply_leadership(False)

    def shutdown(self, wait=True):
        """关闭调度器"""
        m.add_log('system', 0, 'info',
                   f'🔴 Scheduler closed: {self.scheduler_id}')
        self._apscheduler.shutdown(wait=wait)

    def pause(self):
        """暂停所有任务"""
        self._apscheduler.pause()
        m.add_log('system', 0, 'warn', _('⏸️ Scheduler paused'))

    def resume(self):
        """恢复所有任务"""
        self._apscheduler.resume()
        # A6：非 leader 副本不得因人工 resume 而恢复业务作业，否则重新出现重复触发
        if self._leadership_checked and not self._is_leader:
            self._set_business_jobs_suspended(True)
        m.add_log('system', 0, 'info', _('▶️ Scheduler resumed'))

    # ---- A6：选主与作业挂起 ----

    def _running_job_count(self) -> int:
        """当前有下次执行时间的运行中作业数（心跳/观测用）"""
        return len([j for j in self._running_jobs.values()
                    if j and getattr(j, 'next_run_time', None)])

    def _leader_election_enabled(self) -> bool:
        """A6 回退开关（缝位 ④）：system_config.scheduler_leader_election
        或环境变量 SCHEDULER_LEADER_ELECTION。

        显式 '0'/'false'/'off'/'no' → 关闭选主，回退为"每副本都调度"的旧行为
        （仅供单副本部署省事；多副本下会重新引入重复触发，属临时手段）。
        未配置 / 读取失败 → 默认开启（多副本安全优先）。"""
        raw = os.environ.get('SCHEDULER_LEADER_ELECTION')
        if raw is None:
            try:
                with m.get_db() as conn:
                    conn.execute(
                        "SELECT value FROM system_config WHERE key=%s",
                        (self.SCHEDULER_LEADER_ELECTION_KEY,))
                    row = conn.fetchone()
                raw = (row or {}).get('value')
            except Exception:
                raw = None
        if raw is None or str(raw).strip() == '':
            return True
        return str(raw).strip().lower() not in ('0', 'false', 'off', 'no')

    def _try_acquire_leadership(self) -> bool:
        """A6：行级 CAS 抢主（单条语句原子，N 个副本只有一个能拿到行）。

        语义：holder 为空 / 租约已过期 / holder 就是自己 → 抢到并续租；
        否则抢不到。返回 True 表示本副本当前持有 leader 租约。
        异常时返回 False（fail-closed：拿不到租约就不调度，宁可停不可重复）。"""
        if not self._leader_election_enabled():
            return True
        hostname = platform.node() or os.environ.get('COMPUTERNAME', 'localhost')
        try:
            with m.get_db() as conn:
                conn.execute("""
                    INSERT INTO scheduler_leader
                        (name, holder, expires_at, updated_at)
                    VALUES (%s, %s, NOW() + make_interval(secs => %s::int), NOW())
                    ON CONFLICT (name) DO UPDATE SET
                        holder = EXCLUDED.holder,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = NOW()
                    WHERE scheduler_leader.holder = EXCLUDED.holder
                       OR scheduler_leader.expires_at < NOW()
                    RETURNING holder
                """, (self.SCHEDULER_LEADER_NAME, self.scheduler_id,
                      self.SCHEDULER_LEADER_TTL_SECONDS))
                row = conn.fetchone()
                got = bool(row and row.get('holder') == self.scheduler_id)

                # 真实 leader 状态写回心跳表供运维观测（原实现恒为 1）
                conn.execute("""
                    INSERT INTO scheduler_state
                        (scheduler_id, hostname, is_leader, last_heartbeat,
                         running_jobs, state_json)
                    VALUES (%s,%s,%s, NOW(), %s, '{}')
                    ON CONFLICT (scheduler_id) DO UPDATE SET
                        hostname = EXCLUDED.hostname,
                        is_leader = EXCLUDED.is_leader,
                        last_heartbeat = EXCLUDED.last_heartbeat,
                        running_jobs = EXCLUDED.running_jobs,
                        state_json = EXCLUDED.state_json
                """, (self.scheduler_id, hostname, 1 if got else 0,
                      self._running_job_count()))
            return got
        except Exception as e:
            logger.error(f'[Scheduler] leadership probe failed: {e}')
            return False

    def _set_business_jobs_suspended(self, suspended: bool):
        """挂起/恢复"业务作业"，**永不触碰心跳作业**。

        ⚠️ 不能改用 self._apscheduler.pause()：心跳作业注册在同一个 scheduler 上，
        整体 pause 会让心跳停摆，本副本将永远无法重新抢回 leader（自锁）。"""
        switched = 0
        for job in self._apscheduler.get_jobs():
            if job.id == self.HEARTBEAT_JOB_ID:
                continue
            try:
                if suspended:
                    self._apscheduler.pause_job(job.id)
                else:
                    self._apscheduler.resume_job(job.id)
                switched += 1
            except Exception as e:
                logger.warning(f'[Scheduler] leadership switch failed for {job.id}: {e}')
        return switched

    def _apply_leadership(self, leader: bool):
        """按 leader 状态挂起/恢复业务作业，并记录状态（供 add_plugin_job 等使用）"""
        if self._leadership_checked and leader == self._is_leader:
            return
        self._is_leader = leader
        self._leadership_checked = True
        switched = self._set_business_jobs_suspended(not leader)
        if switched:
            m.add_log('system', 0, 'info' if leader else 'warn',
                       (f'🟢 Scheduler leadership acquired ({self.scheduler_id}), '
                        f'{switched} job(s) resumed') if leader else
                       (f'🟡 Scheduler leadership lost ({self.scheduler_id}), '
                        f'{switched} job(s) suspended'))

    # ---- 任务管理 ----

    def _sync_cron_jobs(self):
        """从数据库同步所有活跃的 Cron 任务"""
        with self._lock:
            # 清除现有 APScheduler 作业（保留管理元数据）
            for job_id in list(self._running_jobs.keys()):
                try:
                    self._apscheduler.remove_job(f'cron_{job_id}')
                except Exception:
                    pass
            self._running_jobs.clear()

            # 从数据库重新加载
            result = m.list_cron_jobs(active_only=True, limit=1000)
            for job in result['jobs']:
                self._schedule_job(job)

    def _schedule_job(self, job: dict):
        """调度单个任务"""
        job_id = job['id']
        job_type = job['job_type']
        cron_expr = job.get('cron_expr') or ''
        natural_expr = job.get('natural_expr') or ''
        interval_sec = job.get('interval_seconds', 0)
        start_at = job.get('start_at') or None
        end_at = job.get('end_at') or None
        max_runs = job.get('max_runs', 0)

        # 确定最终 cron 表达式
        if natural_expr and not cron_expr:
            cron_expr = parse_natural_cron(natural_expr)

        try:
            # 创建 APScheduler trigger
            if job_type == 'cron' and cron_expr:
                parts = cron_expr.strip().split()
                if len(parts) == 5:
                    trigger = CronTrigger(
                        minute=parts[0], hour=parts[1],
                        day=parts[2], month=parts[3], day_of_week=parts[4],
                        timezone=job.get('timezone', 'Asia/Shanghai')
                    )
                elif len(parts) == 6:
                    trigger = CronTrigger(
                        second=parts[0], minute=parts[1], hour=parts[2],
                        day=parts[3], month=parts[4], day_of_week=parts[5],
                        timezone=job.get('timezone', 'Asia/Shanghai')
                    )
                else:
                    m.add_log('cron', job_id, 'error', f'Invalid Cron Expression: {cron_expr}')
                    return

            elif job_type == 'interval' and interval_sec > 0:
                trigger = IntervalTrigger(seconds=interval_sec)

            elif job_type == 'once' and start_at:
                trigger = DateTrigger(run_date=start_at)

            else:
                m.add_log('cron', job_id, 'warn', f'Invalid Task Type or Missing Parameters')
                return

            # 添加 APScheduler 作业
            aps_job = self._apscheduler.add_job(
                self._execute_job_wrapper,
                trigger=trigger,
                id=f'cron_{job_id}',
                name=job.get('name', f'Job-{job_id}'),
                args=[job_id],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                replace_existing=True
            )

            self._running_jobs[job_id] = aps_job
            # A6：非 leader 副本新登记的作业立即挂起（抢到租约后由心跳统一恢复）
            if self._leadership_checked and not self._is_leader:
                try:
                    self._apscheduler.pause_job(f'cron_{job_id}')
                except Exception as e:
                    logger.warning(f'[Scheduler] pause cron_{job_id} on non-leader failed: {e}')
            m.add_log('cron', job_id, 'info',
                       f'📅 Task Scheduled: [{job.get("name")}] {cron_expr or natural_expr or f"every {interval_sec} seconds"}')

        except Exception as e:
            m.add_log('cron', job_id, 'error',
                       f'Scheduling task failed [{job.get("name")}]: {str(e)}')

    def _execute_job_wrapper(self, job_id: int):
        """任务执行包装器（记录开始/结束/重试）"""
        job = m.get_cron_job(job_id)
        if not job or not job['is_active']:
            return

        start_time = time.time()
        m.update_cron_job(job_id, {
            'last_run_at': m.now_str(),
            'last_status': 'running'
        })

        # 检查并发限制
        current_runs = m.get_cron_job(job_id)
        if current_runs and current_runs.get('run_count', 0) >= current_runs.get('max_runs', 0) > 0:
            m.add_log('cron', job_id, 'warn', _('⏭️ Reached maximum execution count, skipped'))
            return

        result = self._execute_with_retries(job)

        duration_ms = int((time.time() - start_time) * 1000)
        m.update_cron_job(job_id, {
            'last_status': 'success' if result['success'] else 'failed',
            'last_duration_ms': duration_ms,
        })
        # P2-F08: 原子自增 run_count / fail_count（防并发丢失更新）
        try:
            with m.get_db() as conn:
                conn.execute(
                    "UPDATE cron_jobs SET run_count = run_count + 1, "
                    "fail_count = fail_count + %s "
                    "WHERE id = %s",
                    (0 if result['success'] else 1, job_id)
                )
                conn.commit()
        except Exception:
            pass

        # 检查依赖触发
        if result['success']:
            self._trigger_dependent_jobs(job_id)

    def _execute_with_retries(self, job: dict) -> dict:
        """带重试的任务执行"""
        max_retries = job.get('max_retries', 3)
        retry_delay = job.get('retry_delay', 10)
        backoff = job.get('retry_backoff', 2.0)
        timeout = job.get('timeout_seconds', 300)
        job_id = job['id']
        target_type = job['target_type']
        target_config = m.from_json(job.get('target_config', '{}'))

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                m.add_log('cron', job_id, 'info',
                           f'🔄 Execution Attempt {attempt+1}/{max_retries+1}')

                # 查找处理器
                handler = self._callback_map.get(target_type)
                if handler:
                    result = handler(job, target_config, timeout)
                elif target_type == 'workflow':
                    # 默认工作流执行
                    wf_id = target_config.get('workflow_id')
                    if wf_id and self._workflow_runner:
                        result = self._workflow_runner(
                            wf_id, trigger_type='cron',
                            trigger_config={'cron_job_id': job_id}
                        )
                    else:
                        raise ValueError(f'Workflow executor is not set or worklow_id is missing')
                else:
                    # 直接执行 API 调用
                    result = self._execute_api_target(target_config, timeout)

                if result and result.get('success', True):
                    m.add_log('cron', job_id, 'info', _('✅ Execution successful'))
                    return {'success': True, 'result': result}
                else:
                    last_error = str(result.get('error', 'Unknown error'))

            except TimeoutError:
                last_error = _('⏰ Timeout')
                m.add_log('cron', job_id, 'error', f'⏰ Task timeout ({timeout}s)')
                break  # 超时不重试

            except Exception as e:
                last_error = str(e)
                m.add_log('cron', job_id, 'error',
                           f'❌ Execution Failed (Attempt {attempt+1}): {e}')

            # 指数退避等待（最后一次不等待）
            if attempt < max_retries:
                delay = retry_delay * (backoff ** attempt)
                m.add_log('cron', job_id, 'info',
                           f'⏳ Retry after {delay:.0f} seconds...')
                time.sleep(delay)

        m.add_log('cron', job_id, 'error',
                   f'❌ Execution Failed (Retried {max_retries} Times): {last_error}')
        return {'success': False, 'error': last_error}

    def _execute_api_target(self, config: dict, timeout: int) -> dict:
        """执行 API 类型的任务目标"""
        import urllib.request
        import json

        url = config.get('url', '')
        method = config.get('method', 'GET').upper()
        headers = config.get('headers', {})
        body = config.get('body')

        req = urllib.request.Request(url, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        if body:
            req.data = json.dumps(body).encode('utf-8')
            if 'Content-Type' not in headers:
                req.add_header('Content-Type', 'application/json')

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {
                    'success': resp.status < 400,
                    'status': resp.status,
                    'body': resp.read().decode('utf-8')[:5000]
                }
        except Exception as e:
            raise

    def _trigger_dependent_jobs(self, job_id: int):
        """触发依赖当前任务的后继任务"""
        with m.get_db() as conn:
            conn.execute("""
                SELECT j.* FROM job_dependencies d
                JOIN cron_jobs j ON d.job_id = j.id
                WHERE d.depends_on_job_id = %s AND j.is_active = 1
            """, (job_id,))
            deps_row = conn.fetchall()
            deps = [dict(r) for r in deps_row]

        for dep in deps:
            dep = dict(dep)
            m.add_log('cron', dep['id'], 'info',
                       f'🔗 由任务 #{job_id} 完成触发执行')
            self._execute_job_wrapper(dep['id'])

    # ---- 事件处理 ----

    def _on_job_event(self, event):
        """APScheduler 事件回调"""
        # 状态由 _execute_job_wrapper 记录，这里只记录额外异常
        if event.exception:
            m.add_log('cron', 0, 'error',
                       f'APScheduler exception: {event.exception}')

    def _register_scheduler_heartbeat(self):
        """注册调度器心跳"""
        self._apscheduler.add_job(
            self._heartbeat,
            trigger=IntervalTrigger(seconds=30),
            id='_scheduler_heartbeat',
            name='Scheduler Heartbeat',
            replace_existing=True,
            max_instances=1
        )

    def _heartbeat(self):
        """调度器心跳：A6 选主续租 + 按 leader 状态挂起/恢复业务作业。

        原实现写死 is_leader=1（心跳注册表而非选主）→ N 个副本 = N 个 leader
        → 同一 cron 被重复触发。现由 _try_acquire_leadership 续租。"""
        try:
            got = self._try_acquire_leadership()
            self._apply_leadership(got)
        except Exception as e:
            logger.error(f"[Scheduler Heartbeat] Failed to update heartbeat: {e}")

    # ---- 外部接口 ----

    def add_job(self, job_data: dict) -> int:
        """从外部添加新任务（自动调度）"""
        job_id = m.create_cron_job(job_data)
        self._sync_cron_jobs()
        return job_id

    def remove_job(self, job_id: int) -> bool:
        """移除任务并取消调度"""
        result = m.delete_cron_job(job_id)
        if result:
            try:
                self._apscheduler.remove_job(f'cron_{job_id}')
                self._running_jobs.pop(job_id, None)
            except Exception:
                pass
        return result

    def update_job(self, job_id: int, data: dict) -> bool:
        """更新任务并重新调度"""
        result = m.update_cron_job(job_id, data)
        if result:
            self._sync_cron_jobs()
        return result

    def pause_job(self, job_id: int) -> bool:
        """暂停单个任务"""
        return self.update_job(job_id, {'is_active': 0})

    def resume_job(self, job_id: int) -> bool:
        """恢复单个任务"""
        return self.update_job(job_id, {'is_active': 1})

    def get_status(self) -> dict:
        """获取调度器状态"""
        from apscheduler.schedulers.base import STATE_PAUSED
        return {
            'scheduler_id': self.scheduler_id,
            'running': self._apscheduler.running,
            'paused': self._apscheduler.state == STATE_PAUSED,
            'scheduled_jobs': len(self._running_jobs),
            'next_jobs': [
                {
                    'id': jid,
                    'name': getattr(j, 'name', ''),
                    'next_run': str(j.next_run_time) if getattr(j, 'next_run_time', None) else None
                }
                for jid, j in self._running_jobs.items()
                if j and getattr(j, 'next_run_time', None)
            ][:20]
        }


# ============================================================
# 命令行测试
# ============================================================

if __name__ == '__main__':
    m.init_orchestrator_tables()

    scheduler = SchedulerEngine()
    scheduler.start()

    logger.info('Scheduler started: %s', scheduler.scheduler_id)
    logger.info('Press Ctrl+C to stop.')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        logger.info('Scheduler stopped.')
