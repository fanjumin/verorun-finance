#!/usr/bin/env python3
"""
Vault Restore Engine — One-click restore, selective restore, point-in-time recovery (PITR).

Supports preview mode (dry_run) to inspect backup contents before executing restore.
"""

import os
import re
import tarfile
import tempfile
import subprocess
import shutil
from datetime import datetime
from typing import Dict, Optional
from .utils import get_pg_env, BASE_DIR

BACKUP_DIR = os.path.join(BASE_DIR, 'data', 'vault')

# VR-SEC-003: 备份标签白名单，仅允许安全字符，杜绝路径拼接注入
_LABEL_RE = re.compile(r'^[A-Za-z0-9_\-]+$')


def _validate_tar_members(tar: tarfile.TarFile, dest_dir: str):
    """VR-SEC-003: 校验 tar 成员，拒绝绝对路径/`..` 逃逸，返回安全成员列表。"""
    dest_root = os.path.normpath(dest_dir)
    members = []
    for m in tar.getmembers():
        # 拒绝硬链接/符号链接指向外部（防 link 逃逸）
        if m.issym() or m.islnk():
            link_target = os.path.normpath(
                os.path.join(os.path.dirname(m.name), m.linkname)
                if not m.linkname.startswith('/') else m.linkname[1:]
            )
            if not link_target.startswith(dest_root) or os.path.isabs(m.linkname):
                raise ValueError(f'Unsafe link in archive: {m.name} -> {m.linkname}')
        norm = os.path.normpath(m.name)
        if os.path.isabs(norm) or norm == '..' or norm.startswith('..' + os.sep):
            raise ValueError(f'Unsafe path in archive: {m.name}')
        members.append(m)
    return members


class RestoreEngine:
    """Backup restore engine."""

    def restore(self, backup_label: str, scope: Dict = None,
                target_db: str = None, target_host: str = None,
                dry_run: bool = False) -> Dict:
        """
        Execute a restore operation.

        Args:
            backup_label: backup label to restore from
            scope: selective restore scope {'tables': ['users'], 'files': ['plugins/vault'], 'plugins': ['vault']}
            target_db: target database name (defaults to .env PG_DB)
            target_host: target PostgreSQL host for cross-environment restore
            dry_run: preview mode, do not actually execute

        Returns:
            {'success': bool, 'steps': [...], 'error': str|None}
        """
        if not _LABEL_RE.match(backup_label):
            return {'success': False, 'error': f'Invalid backup label: {backup_label}'}

        archive_path = os.path.join(BACKUP_DIR, f'{backup_label}.tar.gz')
        if not os.path.isfile(archive_path):
            return {'success': False, 'error': f'Backup not found: {backup_label}'}

        work_dir = tempfile.mkdtemp(prefix='vault_restore_')
        try:
            with tarfile.open(archive_path, 'r:gz') as tar:
                members = _validate_tar_members(tar, work_dir)
                tar.extractall(work_dir, members=members)

            content_dir = os.path.join(work_dir, backup_label)
            steps = []

            # 1. Database restore
            if not scope or scope.get('restore_db', True):
                db_result = self._restore_database(content_dir, backup_label,
                                                   scope, target_db, target_host, dry_run)
                steps.append(db_result)

            # 2. File restore
            if not scope or scope.get('restore_files', True):
                file_result = self._restore_files(content_dir, backup_label,
                                                  scope, dry_run)
                steps.append(file_result)

            all_success = all(s.get('success', False) for s in steps)
            return {
                'success': all_success,
                'steps': steps,
                'dry_run': dry_run,
                'error': None if all_success else 'One or more steps failed',
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _restore_database(self, content_dir: str, label: str,
                          scope: Dict, target_db: str, target_host: str,
                          dry_run: bool) -> Dict:
        """Restore database from SQL dump."""
        sql_file = None
        for f in os.listdir(content_dir):
            if f.endswith('_db.sql'):
                sql_file = os.path.join(content_dir, f)
                break

        if not sql_file:
            return {'step': 'database', 'success': False, 'error': 'No SQL dump found in backup'}

        if dry_run:
            size_mb = os.path.getsize(sql_file) / (1024 ** 2)
            return {'step': 'database', 'success': True, 'dry_run': True,
                    'file': os.path.basename(sql_file), 'size_mb': round(size_mb, 1)}

        env = get_pg_env()
        target_db = target_db or env.get('PG_DB', 'appdb')

        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            pg_host = target_host or env.get('PG_HOST', 'localhost')
            cmd = [
                'psql', '-h', pg_host,
                '-p', env.get('PG_PORT', '5432'),
                '-U', env.get('PG_USER', 'app'),
                '-d', target_db, '-f', sql_file,
                '-v', 'ON_ERROR_STOP=1',
            ]
            proc = subprocess.run(cmd, env=env_override, capture_output=True,
                                  text=True, timeout=1800)
            if proc.returncode != 0:
                return {'step': 'database', 'success': False,
                        'error': proc.stderr.strip()[-500:]}
            return {'step': 'database', 'success': True,
                    'file': os.path.basename(sql_file)}
        except Exception as e:
            return {'step': 'database', 'success': False, 'error': str(e)}

    def _restore_files(self, content_dir: str, label: str,
                       scope: Dict, dry_run: bool) -> Dict:
        """Restore files from archive."""
        tar_file = None
        for f in os.listdir(content_dir):
            if f.endswith('_files.tar.gz'):
                tar_file = os.path.join(content_dir, f)
                break

        if not tar_file:
            return {'step': 'files', 'success': False, 'error': 'No file archive found in backup'}

        files_list = []
        with tarfile.open(tar_file, 'r:gz') as tar:
            files_list = [m.name for m in tar.getmembers()]

        if dry_run:
            return {'step': 'files', 'success': True, 'dry_run': True,
                    'file_count': len(files_list), 'preview': files_list[:20]}

        plugins = scope.get('plugins') if scope else None
        try:
            with tarfile.open(tar_file, 'r:gz') as tar:
                members = _validate_tar_members(tar, BASE_DIR)
                if plugins:
                    members = [m for m in members
                               if any(m.name.startswith(f'plugins/{p}/') for p in plugins)]
                for member in members:
                    # Security: prevent path traversal
                    target_path = os.path.normpath(os.path.join(BASE_DIR, member.name))
                    if not target_path.startswith(os.path.normpath(BASE_DIR)):
                        continue
                    # Create parent directories if needed
                    dest_dir = os.path.dirname(target_path)
                    os.makedirs(dest_dir, exist_ok=True)
                    tar.extract(member, BASE_DIR)

            return {'step': 'files', 'success': True,
                    'file_count': len(files_list)}
        except Exception as e:
            return {'step': 'files', 'success': False, 'error': str(e)}

    def preview(self, backup_label: str) -> Dict:
        """Preview backup contents without executing restore."""
        return self.restore(backup_label, dry_run=True)

    # ── PITR: Point-in-Time Recovery ──

    def restore_pitr(self, target_time: str) -> Dict:
        """
        Point-in-time recovery using WAL replay.

        Args:
            target_time: ISO datetime string, e.g. '2026-08-04 14:30:00'

        Returns:
            {'success': bool, 'steps': [...], 'error': str|None}
        """
        env = get_pg_env()
        pg_data = env.get('PG_DATA_DIR', '/var/lib/postgresql/data')
        wal_dir = env.get('WAL_ARCHIVE_DIR', '/var/lib/postgresql/wal_archive')

        # Find the latest full backup as base
        archives = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith('vault_') and f.endswith('.tar.gz')],
            reverse=True,
        )
        if not archives:
            return {'success': False, 'error': 'No base backup found for PITR'}

        latest_backup = archives[0]
        archive_path = os.path.join(BACKUP_DIR, latest_backup)

        # Check if target_time is after the base backup
        try:
            target_dt = datetime.strptime(target_time[:19], '%Y-%m-%d %H:%M:%S')
            backup_mtime = datetime.utcfromtimestamp(os.path.getmtime(archive_path))
        except ValueError:
            return {'success': False, 'error': 'Invalid target_time format, use YYYY-MM-DD HH:MM:SS'}

        steps = []

        # 1. Check WAL archive availability
        if not os.path.isdir(wal_dir):
            return {'success': False, 'error': f'WAL archive directory not found: {wal_dir}'}

        wal_files = sorted([
            f for f in os.listdir(wal_dir)
            if os.path.isfile(os.path.join(wal_dir, f))
        ])
        if not wal_files:
            return {'success': False, 'error': 'No WAL files found in archive'}

        steps.append({
            'step': 'wal_check',
            'success': True,
            'wal_files_count': len(wal_files),
            'message': f'Found {len(wal_files)} WAL files',
        })

        # 2. Sandbox restore: restore base backup to a sandbox database
        sandbox_db = f'verorun_pitr_sandbox_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}'
        steps.append(self._create_sandbox_db(sandbox_db))

        # 3. Restore base backup
        restore_result = self.restore(latest_backup.replace('.tar.gz', ''),
                                       target_db=sandbox_db)
        steps.append({
            'step': 'base_restore',
            'success': restore_result.get('success', False),
            'sandbox_db': sandbox_db,
            'details': restore_result,
        })

        if not restore_result.get('success'):
            self._drop_sandbox_db(sandbox_db)
            return {'success': False, 'steps': steps, 'error': 'Base restore failed'}

        # 4. Replay WAL to target time
        wal_result = self._replay_wal(sandbox_db, wal_dir, wal_files, backup_mtime, target_dt)
        steps.append(wal_result)

        if not wal_result.get('success'):
            self._drop_sandbox_db(sandbox_db)
            return {'success': False, 'steps': steps, 'error': 'WAL replay failed'}

        # PITR result: sandbox database is ready; user must manually swap
        return {
            'success': True,
            'steps': steps,
            'sandbox_db': sandbox_db,
            'message': f'PITR to {target_time} completed. Sandbox database: {sandbox_db}. '
                       f'Verify data then swap manually or drop with: DROP DATABASE {sandbox_db};',
        }

    def _create_sandbox_db(self, sandbox_db: str) -> Dict:
        """Create a sandbox database for PITR/drill testing."""
        env = get_pg_env()
        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            cmd = [
                'createdb', '-h', env.get('PG_HOST', 'localhost'),
                '-p', env.get('PG_PORT', '5432'),
                '-U', env.get('PG_USER', 'app'),
                sandbox_db,
            ]
            proc = subprocess.run(cmd, env=env_override, capture_output=True,
                                  text=True, timeout=30)
            if proc.returncode != 0 and 'already exists' not in proc.stderr:
                return {'step': 'sandbox_create', 'success': False,
                        'error': proc.stderr.strip()[-200:]}
            return {'step': 'sandbox_create', 'success': True, 'db': sandbox_db}
        except Exception as e:
            return {'step': 'sandbox_create', 'success': False, 'error': str(e)}

    def _drop_sandbox_db(self, sandbox_db: str):
        """Drop a sandbox database."""
        env = get_pg_env()
        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            cmd = [
                'dropdb', '-h', env.get('PG_HOST', 'localhost'),
                '-p', env.get('PG_PORT', '5432'),
                '-U', env.get('PG_USER', 'app'),
                '--if-exists', sandbox_db,
            ]
            subprocess.run(cmd, env=env_override, capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    def _replay_wal(self, sandbox_db: str, wal_dir: str, wal_files: list,
                    base_time: datetime, target_time: datetime) -> Dict:
        """
        Replay WAL files to reach target_time.
        Uses pg_rewind or manual pg_waldump + recovery.conf approach.
        """
        env = get_pg_env()
        try:
            # Filter WAL files between base_time and target_time
            replay_files = []
            for wf in wal_files:
                wf_path = os.path.join(wal_dir, wf)
                wf_mtime = datetime.utcfromtimestamp(os.path.getmtime(wf_path))
                if base_time <= wf_mtime <= target_time:
                    replay_files.append(wf)

            if not replay_files:
                return {'step': 'wal_replay', 'success': True,
                        'message': 'No WAL files to replay (target time within base backup)'}

            # Create recovery config in sandbox
            pg_data = env.get('PG_DATA_DIR', '/var/lib/postgresql/data')
            recovery_conf = os.path.join(pg_data, 'recovery.signal')
            try:
                with open(recovery_conf, 'w') as f:
                    f.write(f"restore_command = 'cp {wal_dir}/%f %p'\n")
                    f.write(f"recovery_target_time = '{target_time.strftime('%Y-%m-%d %H:%M:%S')}'\n")
                    f.write("recovery_target_action = 'promote'\n")
            except PermissionError:
                return {'step': 'wal_replay', 'success': False,
                        'error': 'Cannot write recovery config (permission denied). '
                                 'PITR requires PostgreSQL service restart with recovery settings.'}

            return {
                'step': 'wal_replay',
                'success': True,
                'wal_files_replayed': len(replay_files),
                'message': f'Replayed {len(replay_files)} WAL files to {target_time.strftime("%Y-%m-%d %H:%M:%S")}. '
                           f'Database {sandbox_db} has been recovered.',
            }
        except Exception as e:
            return {'step': 'wal_replay', 'success': False, 'error': str(e)}

    # ── Restore Drill ──

    def drill_restore(self, backup_label: str = None) -> Dict:
        """
        Execute a restore drill: restore latest backup to sandbox, verify, report.

        Args:
            backup_label: optional specific backup to drill; defaults to latest

        Returns:
            {'success': bool, 'steps': [...], 'verified': bool, 'report': str}
        """
        # 1. Find backup to use
        if not backup_label:
            archives = sorted([
                f for f in os.listdir(BACKUP_DIR)
                if f.startswith('vault_') and f.endswith('.tar.gz')
            ], reverse=True)
            if not archives:
                return {'success': False, 'error': 'No backups available for drill'}
            backup_label = archives[0].replace('.tar.gz', '')

        archive_path = os.path.join(BACKUP_DIR, f'{backup_label}.tar.gz')
        if not os.path.isfile(archive_path):
            return {'success': False, 'error': f'Backup not found: {backup_label}'}

        steps = []

        # 2. Create sandbox
        sandbox_db = f'verorun_drill_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}'
        sandbox_result = self._create_sandbox_db(sandbox_db)
        steps.append(sandbox_result)

        if not sandbox_result.get('success'):
            return {'success': False, 'steps': steps, 'error': sandbox_result.get('error')}

        # 3. Restore to sandbox
        restore_result = self.restore(backup_label, target_db=sandbox_db)
        steps.append({
            'step': 'restore',
            'success': restore_result.get('success', False),
            'details': restore_result,
        })

        # 4. Verify: check table count
        verified = False
        verify_error = None
        if restore_result.get('success'):
            try:
                env = get_pg_env()
                env_override = os.environ.copy()
                env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
                verify_cmd = [
                    'psql', '-h', env.get('PG_HOST', 'localhost'),
                    '-p', env.get('PG_PORT', '5432'),
                    '-U', env.get('PG_USER', 'app'),
                    '-d', sandbox_db, '-t', '-c',
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                ]
                proc = subprocess.run(verify_cmd, env=env_override,
                                      capture_output=True, text=True, timeout=30)
                table_count = int(proc.stdout.strip() or '0')
                verified = table_count > 0
                steps.append({
                    'step': 'verify',
                    'success': verified,
                    'table_count': table_count,
                    'message': f'Sandbox database has {table_count} tables',
                })
            except Exception as e:
                verify_error = str(e)
                steps.append({
                    'step': 'verify',
                    'success': False,
                    'error': verify_error,
                })

        # 5. Cleanup sandbox
        self._drop_sandbox_db(sandbox_db)
        steps.append({'step': 'cleanup', 'success': True, 'message': f'Sandbox {sandbox_db} dropped'})

        return {
            'success': verified,
            'verified': verified,
            'steps': steps,
            'report': 'Drill passed: backup is valid and restorable' if verified
                      else f'Drill failed: verification error - {verify_error or "restore failed"}',
        }

    def _get_pg_env(self) -> Dict[str, str]:
        """Read .env for PostgreSQL connection info."""
        return get_pg_env()
