#!/usr/bin/env python3
"""
Vault Scheduler — Cron expression scheduling with backup window + pre/post hooks.

Manages backup schedules, computes next run times, and triggers backup jobs.
"""

import subprocess
import os
import shlex
import glob as _glob
from datetime import datetime, time, timedelta
from croniter import croniter
from .utils import get_vault_conn


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

        # 3. Post-hook
        if schedule.get('post_hook') and backup_result['success']:
            self._run_hook(schedule['post_hook'])

        # 4. Cleanup expired backups
        if schedule.get('retention_days') or schedule.get('retention_count'):
            self._cleanup_old_backups(
                schedule['retention_days'],
                schedule['retention_count'],
            )

        # 5. Update schedule status
        self._update_schedule_status(schedule['id'])

        result['success'] = backup_result['success']
        return result

    def run_all_due(self) -> list:
        """Execute all due schedules, return result list. Failed schedules get backoff."""
        due = self.get_due_schedules()
        results = []
        for sched in due:
            max_retries = sched.get('max_retries', 3)
            retry_delay = sched.get('retry_delay', 60)
            result = None
            for attempt in range(max_retries):
                try:
                    result = self.execute_schedule(sched)
                    results.append(result)
                    if result['success']:
                        break
                except Exception as e:
                    result = {
                        'schedule_id': sched['id'],
                        'success': False,
                        'error': str(e),
                    }
                    if attempt < max_retries - 1:
                        print(f'[Vault] Schedule {sched["id"]} attempt {attempt+1} failed, retrying in {retry_delay}s...')
                        __import__('time').sleep(retry_delay)
                if not result['success'] and attempt == max_retries - 1:
                    results.append(result)
                elif not result['success']:
                    continue
            # If all retries failed, apply backoff
            if result and not result['success']:
                try:
                    self._update_schedule_status_with_backoff(sched['id'], minutes=5)
                except Exception:
                    pass
        return results

    def _update_schedule_status_with_backoff(self, schedule_id: int, minutes: int = 5):
        """Update next_run_at with backoff delay after failure."""
        conn = get_vault_conn()
        cur = conn.cursor()
        next_run = datetime.utcnow() + timedelta(minutes=minutes)
        cur.execute("""
            UPDATE vault_schedules
            SET last_run_at = %s, next_run_at = %s
            WHERE id = %s
        """, (datetime.utcnow(), next_run, schedule_id))
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
        """Remove old backups based on retention policy (by count and/or age)."""
        if not retention_days and not retention_count:
            return
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  '..', '..', '..', 'data', 'vault')
        if not os.path.isdir(backup_dir):
            return
        archives = sorted(
            _glob.glob(os.path.join(backup_dir, 'vault_*.tar.gz')),
            key=os.path.getmtime, reverse=True,
        )

        keep_count = retention_count or len(archives)
        cutoff_time = (datetime.utcnow().timestamp() - retention_days * 86400
                       if retention_days else 0)

        for i, archive in enumerate(archives):
            # Keep newest N
            if i < keep_count:
                continue
            # Keep within retention days
            if retention_days and os.path.getmtime(archive) >= cutoff_time:
                continue
            # Delete
            try:
                os.remove(archive)
                print(f'[Vault] Cleaned up: {os.path.basename(archive)}')
            except OSError as e:
                print(f'[Vault] Cleanup failed for {archive}: {e}')

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
        cur.execute("""
            INSERT INTO vault_schedules
                (name, cron_expression, backup_type, retention_days, retention_count,
                 storage_targets, backup_window, pre_hook, post_hook, enabled,
                 next_run_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,NOW())
            RETURNING id
        """, (
            name, cron_expr, backup_type,
            retention_days, retention_count,
            _json.dumps(storage_targets or []),
            _json.dumps(backup_window) if backup_window else None,
            pre_hook, post_hook, next_run,
        ))
        row_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        # Return the created schedule
        cur = conn.cursor()
        cur.execute("SELECT * FROM vault_schedules WHERE id = %s", (row_id,))
        row = cur.fetchone()
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
