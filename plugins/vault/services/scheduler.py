#!/usr/bin/env python3
"""
Vault Scheduler — Cron expression scheduling with backup window + pre/post hooks.

Manages backup schedules, computes next run times, and triggers backup jobs.
"""

import subprocess
import os
import json
import shlex
from datetime import datetime, time, timedelta
from croniter import croniter
from .utils import get_vault_conn, load_plugin_config, list_backup_files


# ── Retry policy (SD-6 / F-C) ──────────────────────────────────────────────
# 重试不允许在单次 cron 调用内阻塞 sleep（执行器每分钟启动一个独立进程，
# 阻塞 60s 会与下一分钟的进程叠加，并发处理同一到期计划）。改为跨 tick 重试：
# 失败后把 next_run_at 推进一个 tick，由后续分钟的进程接手。
SCHEDULE_RETRY_DELAY_SECONDS = 60
SCHEDULE_MAX_RETRIES = 3
# 错过某个 cron 槽位后的重试总窗口。配合 60s 的 tick，等价于最多 3 次跨 tick
# 重试；超出窗口直接跳到下一个正常 cron 时刻。无需持久化重试计数（不加列）。
SCHEDULE_RETRY_WINDOW_SECONDS = SCHEDULE_RETRY_DELAY_SECONDS * SCHEDULE_MAX_RETRIES


def _compute_retry_time(now: datetime, cron_expr: str, next_cron_run):
    """失败后的下次执行时间（纯函数，便于测试）。

    - 仍在重试窗口内：now + 一个 tick（下一分钟的调度进程会重新拾取）；
    - 已超出窗口：跳到下一个正常 cron 时刻，避免无限重试与告警刷屏；
    - cron 表达式缺失或非法：保守地按一个 tick 后重试。
    """
    retry_at = now + timedelta(seconds=SCHEDULE_RETRY_DELAY_SECONDS)
    if not cron_expr:
        return retry_at
    try:
        missed_slot = croniter(cron_expr, now).get_prev(datetime)
    except Exception:
        return retry_at
    if (now - missed_slot).total_seconds() >= SCHEDULE_RETRY_WINDOW_SECONDS:
        return next_cron_run
    return retry_at


# 命令白名单 — 仅允许在 hook 中执行的可执行文件（绝对路径）。
# 可用环境变量 VAULT_HOOK_ALLOWLIST 追加（以空格或冒号分隔的绝对路径）。
_DEFAULT_ALLOWED_HOOK_COMMANDS = {
    '/usr/bin/pg_dump',
    '/usr/bin/pg_restore',
    '/usr/bin/tar',
    '/usr/bin/gzip',
    '/usr/bin/zstd',
    '/usr/bin/curl',
    '/usr/bin/wget',
    '/usr/bin/rsync',
    '/usr/local/bin/verorun-backup-helper',
}


def _load_allowed_hook_commands() -> set:
    """合并内置白名单与环境变量扩展白名单。"""
    allowed = set(_DEFAULT_ALLOWED_HOOK_COMMANDS)
    extra = os.environ.get('VAULT_HOOK_ALLOWLIST', '')
    if extra:
        for item in extra.replace(':', ' ').split():
            if item.strip():
                allowed.add(item.strip())
    return allowed


def _validate_hook_command(cmd_str: str, allowed: set) -> list:
    """验证并解析 hook 命令，防止命令注入。

    - 使用 shlex.split 安全分词，不解析 shell 元字符（; | & > 等）
    - 仅允许白名单中的可执行文件（os.path.realpath 防符号链接绕过）
    """
    if not cmd_str or not cmd_str.strip():
        return []

    try:
        parts = shlex.split(cmd_str)
    except ValueError as e:
        raise ValueError(f'Invalid hook command: {e}')

    if not parts:
        return []

    executable = parts[0]
    real_exe = os.path.realpath(executable) if os.path.exists(executable) else executable
    if real_exe not in allowed:
        raise ValueError(
            f"Hook command '{executable}' is not in the allowed list. "
            f"Allowed: {sorted(allowed)}"
        )

    return parts


class VaultScheduler:
    """Manage backup schedules, compute next run times, trigger backup jobs."""

    def __init__(self):
        self._engine = None  # lazy init

    def get_all_schedules(self) -> list:
        """Get all enabled schedules."""
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, cron_expression, backup_type, retention_days,
                   retention_count, storage_targets, backup_window,
                   pre_hook, post_hook, enabled, last_run_at, next_run_at
            FROM vault_schedules
            WHERE enabled = TRUE
            ORDER BY next_run_at ASC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [self._row_to_dict(row) for row in rows]

    def get_due_schedules(self) -> list:
        """Get all schedules that are due for execution."""
        now = datetime.utcnow()
        schedules = self.get_all_schedules()
        return [s for s in schedules
                if s['next_run_at'] and s['next_run_at'] <= now]

    def compute_next_run(self, cron_expr: str,
                         backup_window: dict = None) -> datetime:
        """Compute next execution time, respecting backup window. Max 100 iterations."""
        base_time = datetime.utcnow()
        cron = croniter(cron_expr, base_time)
        max_iterations = 100

        for _ in range(max_iterations):
            next_run = cron.get_next(datetime)

            if not backup_window:
                return next_run

            window_start = self._parse_time(backup_window.get('start', '00:00'))
            window_end = self._parse_time(backup_window.get('end', '23:59'))

            if window_start <= next_run.time() <= window_end:
                return next_run

            # Adjust to window start
            next_run = next_run.replace(
                hour=window_start.hour, minute=window_start.minute,
                second=0, microsecond=0,
            )
            if next_run > base_time:
                return next_run

        # Max iterations reached, return raw cron value
        print(f'[Vault] compute_next_run: max iterations reached for {cron_expr}')
        return cron.get_next(datetime)

    def execute_schedule(self, schedule: dict) -> dict:
        """Execute a schedule: run pre-hook → backup → post-hook → cleanup → update status."""
        from .backup_engine import BackupEngine

        result = {'schedule_id': schedule['id'], 'success': False}

        # 1. Pre-hook
        if schedule.get('pre_hook'):
            hook_result = self._run_hook(schedule['pre_hook'])
            if not hook_result['success']:
                result['error'] = f"pre_hook failed: {hook_result['error']}"
                return result

        # 2. Execute backup
        engine = BackupEngine()
        backup_result = engine.create_backup(backup_type=schedule['backup_type'])
        result['backup'] = backup_result

        # 2b. 后置管线（轨 A）：压缩/加密 → 上传 → 通知 → 落库 → 审计，
        #     与手动备份 _handle_backup_create 同口径；成败都留痕。
        self._post_backup_pipeline(backup_result)

        # 3. Post-hook
        if schedule.get('post_hook') and backup_result['success']:
            self._run_hook(schedule['post_hook'])

        # 4. Cleanup expired backups
        if schedule.get('retention_days') or schedule.get('retention_count'):
            self._cleanup_old_backups(
                schedule['retention_days'],
                schedule['retention_count'],
            )

        # 5. 成功才推进到下一个正常 cron 时刻；失败由 run_all_due 统一退避，
        #    避免成功路径与重试路径互相覆盖 next_run_at。
        if backup_result['success']:
            self._update_schedule_status(schedule['id'])

        result['success'] = backup_result['success']
        return result

    def _post_backup_pipeline(self, backup_result: dict) -> dict:
        """定时备份后置管线（与手动备份 _handle_backup_create 同口径，轨 A）。

        压缩/加密 finalize → 上传最终产物(best-effort) → 通知 → 落库
        vault_backups → 审计。finalize 失败会把备份置为失败（半成品绝不
        当作成功推进 cron）；上传/通知/落库/审计各自吞异常，不改变备份本身
        成败。create_backup 本身失败时跳过 finalize/上传，但仍通知、落失败行、
        审计，保证定时任务成败在列表/健康分中可见。
        """
        if backup_result.get('archive') and backup_result.get('success'):
            try:
                cfg = load_plugin_config()
            except Exception:
                cfg = {}

            # 1. Finalize: compression -> encryption，回写最终产物与真实元数据
            try:
                from .backup_engine import finalize_artifact
                finalized = finalize_artifact(backup_result['archive'], cfg)
                backup_result['archive'] = finalized['archive']
                backup_result['size_mb'] = finalized['size_mb']
                backup_result['checksum_sha256'] = finalized['checksum_sha256']
                backup_result['encrypted'] = finalized['encrypted']
                backup_result['encryption'] = finalized['encryption']
                backup_result['compressed'] = finalized['compressed']
                backup_result['compressed_size_mb'] = finalized.get('compressed_size_mb')
            except Exception as e:
                backup_result['success'] = False
                backup_result['error'] = 'Artifact finalize failed: %s' % e
                print('[Vault] Scheduled artifact finalize failed: %s' % e)

            # 2. 上传最终产物（best-effort：远端失败不改变本地备份成败）
            if backup_result.get('success'):
                archive_path = backup_result['archive']
                try:
                    from .uploader import upload_backup
                    backup_result['remote'] = upload_backup(
                        archive_path, os.path.basename(archive_path))
                except Exception as e:
                    backup_result['remote'] = {'uploaded': False, 'error': str(e)}

        # 3. 通知（成败都发）
        try:
            from .notifier import VaultNotifier
            notifier = VaultNotifier()
            event = 'backup.success' if backup_result.get('success') else 'backup.failed'
            notifier.send(
                event=event,
                message='Scheduled backup %s (%s MB)' % (
                    backup_result.get('label', ''), backup_result.get('size_mb', 0)),
                level='info' if backup_result.get('success') else 'error',
                details=backup_result,
            )
        except Exception as e:
            print('[Vault] Scheduled notify failed: %s' % e)

        # 4. 落库 vault_backups（成败都落，字段针对最终产物）
        self._record_backup(backup_result)

        # 5. 审计（定时任务身份固定为 system）
        try:
            from .audit import log_audit
            log_audit(
                action='backup.scheduled.run',
                resource_type='backup',
                resource_id=backup_result.get('label', ''),
                details={
                    'type': backup_result.get('backup_type', 'full'),
                    'size_mb': backup_result.get('size_mb'),
                    'checksum': backup_result.get('checksum_sha256'),
                    'trigger': 'schedule',
                },
                operator='system',
            )
        except Exception:
            pass
        return backup_result

    def _record_backup(self, backup_result: dict):
        """Insert a vault_backups row reflecting the real final artifact (best-effort)."""
        try:
            conn = get_vault_conn()
            cur = conn.cursor()
            compressed_mb = backup_result.get('compressed_size_mb')
            cur.execute("""
                INSERT INTO vault_backups
                    (label, backup_type, status, size_bytes, compressed_size,
                     encryption, checksum_sha256,
                     content_summary, started_at, completed_at, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                backup_result.get('label'),
                backup_result.get('backup_type', 'full'),
                'success' if backup_result.get('success') else 'failed',
                int(backup_result.get('size_mb', 0) * 1024 * 1024),
                int(compressed_mb * 1024 * 1024) if compressed_mb else None,
                backup_result.get('encryption', 'none'),
                backup_result.get('checksum_sha256'),
                json.dumps({'files': len(backup_result.get('files', [])),
                            'trigger': 'schedule'}),
                datetime.utcnow(),
                datetime.utcnow(),
                'system',
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print('[Vault] Failed to write scheduled backup record: %s' % e)

    def run_all_due(self) -> list:
        """每个 tick 对每张到期计划只执行一次（不阻塞 sleep、不在同 tick 重试）。

        失败的计划由 _update_schedule_status_with_backoff 把 next_run_at
        推进到下一个 tick（60s 后），重试在后续分钟的独立进程中发生，
        最多跨 tick 重试 SCHEDULE_MAX_RETRIES 次后回到正常 cron 节奏。
        """
        due = self.get_due_schedules()
        results = []
        for sched in due:
            try:
                result = self.execute_schedule(sched)
            except Exception as e:
                result = {
                    'schedule_id': sched['id'],
                    'success': False,
                    'error': str(e),
                }
            results.append(result)
            if not result.get('success'):
                print(
                    f'[Vault] Schedule {sched["id"]} failed; retry scheduled in '
                    f'{SCHEDULE_RETRY_DELAY_SECONDS}s (max {SCHEDULE_MAX_RETRIES} '
                    f'cross-tick retries): {result.get("error", "unknown error")}'
                )
                try:
                    self._update_schedule_status_with_backoff(
                        sched['id'], sched.get('cron_expression', ''))
                except Exception as e:
                    print(f'[Vault] Failed to apply backoff for schedule '
                          f'{sched["id"]}: {e}')
        return results

    def _update_schedule_status_with_backoff(self, schedule_id: int,
                                             cron_expr: str = ''):
        """失败退避：窗口内下一 tick 重试，超出窗口跳到下一正常 cron 时刻。"""
        now = datetime.utcnow()
        next_cron_run = None
        if cron_expr:
            try:
                next_cron_run = self.compute_next_run(cron_expr)
            except Exception as e:
                print(f'[Vault] Backoff next-cron compute failed for schedule '
                      f'{schedule_id}: {e}')
        next_run = _compute_retry_time(now, cron_expr, next_cron_run)

        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE vault_schedules
            SET last_run_at = %s, next_run_at = %s
            WHERE id = %s
        """, (now, next_run, schedule_id))
        conn.commit()
        cur.close()
        conn.close()

    def _run_hook(self, hook_command: str) -> dict:
        """安全执行 hook 命令（shell=False + 白名单，防命令注入）。"""
        try:
            allowed = _load_allowed_hook_commands()
            parts = _validate_hook_command(hook_command, allowed)
            if not parts:
                return {'success': True, 'stdout': '', 'stderr': '', 'error': None}

            proc = subprocess.run(
                parts, shell=False, capture_output=True,
                text=True, timeout=300,
            )
            return {
                'success': proc.returncode == 0,
                'stdout': proc.stdout.strip(),
                'stderr': proc.stderr.strip(),
                'error': proc.stderr.strip() if proc.returncode != 0 else None,
            }
        except ValueError as e:
            return {'success': False, 'error': str(e)}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _cleanup_old_backups(self, retention_days: int, retention_count: int):
        """Remove old backups based on retention policy (by count and/or age).

        SD-1 收口：枚举全部最终产物形态（.tar.gz 及其压缩/加密变体），
        而不是只 glob 裸 .tar.gz；删除文件后同步删除 vault_backups 表行，
        避免批次 7 修复的孤儿行问题经定时路径复现。
        """
        if not retention_days and not retention_count:
            return
        try:
            items = list_backup_files()  # 已按 mtime 倒序，每 label 取最终形态
        except Exception as e:
            print(f'[Vault] Cleanup listing failed: {e}')
            return

        keep_count = retention_count or len(items)
        cutoff_time = (datetime.utcnow().timestamp() - retention_days * 86400
                       if retention_days else 0)

        removed_labels = []
        for i, item in enumerate(items):
            # Keep newest N
            if i < keep_count:
                continue
            # Keep within retention days
            if retention_days and item['mtime'] >= cutoff_time:
                continue
            # Delete the final artifact
            try:
                os.remove(item['path'])
                removed_labels.append(item['label'])
                print(f"[Vault] Cleaned up: {item['filename']}")
            except OSError as e:
                print(f"[Vault] Cleanup failed for {item['path']}: {e}")

        if removed_labels:
            try:
                conn = get_vault_conn()
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM vault_backups WHERE label = ANY(%s)",
                    (removed_labels,),
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print(f'[Vault] Cleanup failed to remove DB rows: {e}')

    def _update_schedule_status(self, schedule_id: int):
        conn = get_vault_conn()
        cur = conn.cursor()
        now = datetime.utcnow()
        cron_expr = self._get_schedule_cron(schedule_id)
        next_run = self.compute_next_run(cron_expr) if cron_expr else None
        cur.execute("""
            UPDATE vault_schedules
            SET last_run_at = %s, next_run_at = %s
            WHERE id = %s
        """, (now, next_run, schedule_id))
        conn.commit()
        cur.close()
        conn.close()

    def _get_schedule_cron(self, schedule_id: int) -> str:
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT cron_expression FROM vault_schedules WHERE id = %s",
            (schedule_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else ''

    @staticmethod
    def _parse_time(time_str: str) -> time:
        parts = time_str.strip().split(':')
        return time(hour=int(parts[0]), minute=int(parts[1]))

    @staticmethod
    def _row_to_dict(row) -> dict:
        cols = ['id', 'name', 'cron_expression', 'backup_type', 'retention_days',
                'retention_count', 'storage_targets', 'backup_window',
                'pre_hook', 'post_hook', 'enabled', 'last_run_at', 'next_run_at']
        return dict(zip(cols, row))

    # ── CRUD Methods ──

    def create_schedule(self, name: str, cron_expr: str, backup_type: str = 'full',
                        retention_days: int = None, retention_count: int = None,
                        storage_targets: list = None, backup_window: dict = None,
                        pre_hook: str = None, post_hook: str = None) -> dict:
        """Create a new backup schedule. Returns the created schedule dict."""
        import json as _json
        conn = get_vault_conn()
        cur = conn.cursor()
        next_run = self.compute_next_run(cron_expr, backup_window)
        created_at = datetime.utcnow()
        cur.execute("""
            INSERT INTO vault_schedules
                (name, cron_expression, backup_type, retention_days, retention_count,
                 storage_targets, backup_window, pre_hook, post_hook, enabled,
                 next_run_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
            RETURNING id
        """, (
            name, cron_expr, backup_type,
            retention_days, retention_count,
            _json.dumps(storage_targets or []),
            _json.dumps(backup_window) if backup_window else None,
            pre_hook, post_hook, next_run, created_at,
        ))
        row_id = cur.fetchone()[0]
        # 修复 BK-D1: 回读放在 conn.close() 之前（原实现 close 后复用连接 -> psycopg2.InterfaceError -> 500 但已入库）
        cur.execute("SELECT * FROM vault_schedules WHERE id = %s", (row_id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return self._row_to_dict(row) if row else {'id': row_id}

    def update_schedule(self, schedule_id: int, **kwargs) -> dict:
        """Update a schedule. Supported fields: name, cron_expression, backup_type,
        retention_days, retention_count, enabled, backup_window, pre_hook, post_hook."""
        import json as _json
        conn = get_vault_conn()
        cur = conn.cursor()

        allowed = ['name', 'cron_expression', 'backup_type', 'retention_days',
                   'retention_count', 'enabled', 'backup_window', 'pre_hook', 'post_hook']
        updates = []
        params = []
        for key in allowed:
            if key in kwargs:
                val = kwargs[key]
                if key in ('backup_window',) and isinstance(val, dict):
                    val = _json.dumps(val)
                updates.append(f"{key} = %s")
                params.append(val)

        if 'cron_expression' in kwargs:
            window = kwargs.get('backup_window')
            if window and isinstance(window, str):
                window = _json.loads(window)
            next_run = self.compute_next_run(kwargs['cron_expression'], window)
            updates.append("next_run_at = %s")
            params.append(next_run)

        if not updates:
            return {'success': False, 'error': 'No valid fields to update'}

        params.append(schedule_id)
        cur.execute(
            f"UPDATE vault_schedules SET {', '.join(updates)} WHERE id = %s",
            params,
        )
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True, 'id': schedule_id}

    def delete_schedule(self, schedule_id: int) -> dict:
        """Delete a schedule."""
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM vault_schedules WHERE id = %s", (schedule_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {'success': deleted > 0, 'deleted': deleted}

    def toggle_schedule(self, schedule_id: int, enabled: bool) -> dict:
        """Enable or disable a schedule."""
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE vault_schedules SET enabled = %s WHERE id = %s",
            (enabled, schedule_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return {'success': True, 'id': schedule_id, 'enabled': enabled}
