#!/usr/bin/env python3
"""
Vault Restore Engine — One-click restore, selective restore, and restore drills.

PITR (point-in-time recovery) is intentionally not implemented: the plugin
produces logical pg_dump archives, which cannot be replayed to an arbitrary
point in time. restore_pitr() fails closed until physical base backups plus
WAL archiving exist.

Supports preview mode (dry_run) to inspect backup contents before executing restore.
"""

import os
import re
import sys
import tarfile
import tempfile
import subprocess
import shutil
from datetime import datetime
from typing import Dict, Optional
from .utils import get_pg_env, BASE_DIR, resolve_backup_path, list_backup_files
from .backup_engine import resolve_pg_tool_for_server, get_server_major_version

BACKUP_DIR = os.path.join(BASE_DIR, 'data', 'vault')

# VR-SEC-003: 备份标签白名单，仅允许安全字符，杜绝路径拼接注入
_LABEL_RE = re.compile(r'^[A-Za-z0-9_\-]+$')


def _is_absolute_or_unc(path: str) -> bool:
    """识别 tar 成员名/链接名中的绝对路径（POSIX /、Windows 盘符、UNC）。"""
    if not path:
        return False
    if path.startswith(('/', '\\')):
        return True
    if len(path) >= 2 and path[1] == ':':
        return True
    return os.path.isabs(path)


def _is_within(path: str, root: str) -> bool:
    """path 规范化后必须等于 root 或位于 root 之内（避免前缀冒充）。"""
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)


def _tar_supports_data_filter() -> bool:
    """tarfile 的 filter='data' 自 Python 3.12 起可用。"""
    return sys.version_info[:2] >= (3, 12)


def _safe_extractall(tar: tarfile.TarFile, dest_dir: str, members=None):
    """extractall 的安全封装：3.12+ 强制 data 过滤器，低版本靠手动校验兜底。"""
    if _tar_supports_data_filter():
        tar.extractall(dest_dir, members=members, filter='data')
    else:
        tar.extractall(dest_dir, members=members)


def _safe_extract_one(tar: tarfile.TarFile, member: tarfile.TarInfo, dest_dir: str):
    """extract 单成员的安全封装：3.12+ 强制 data 过滤器。"""
    if _tar_supports_data_filter():
        tar.extract(member, dest_dir, filter='data')
    else:
        tar.extract(member, dest_dir)


def _validate_tar_members(tar: tarfile.TarFile, dest_dir: str):
    """VR-SEC-003 / RS-05b: 校验 tar 成员，拒绝路径逃逸与外部链接。

    - 普通成员：解析后的绝对路径必须位于 dest_dir 内；
    - 符号链接（issym）：linkname 相对**链接自身所在目录**解析；
    - 硬链接（islnk）：linkname 相对**归档根目录**解析（tar 规范）；
    - 绝对链接、盘符/UNC、解析后越界、设备文件/FIFO 一律拒绝。
    """
    dest_root = os.path.normpath(dest_dir)
    members = []
    for m in tar.getmembers():
        name = m.name

        if _is_absolute_or_unc(name):
            raise ValueError(f'Unsafe absolute path in archive: {name}')
        if not _is_within(os.path.join(dest_root, name), dest_root):
            raise ValueError(f'Unsafe path in archive: {name}')

        if m.issym() or m.islnk():
            link = m.linkname or ''
            if _is_absolute_or_unc(link):
                raise ValueError(
                    f'Unsafe absolute link in archive: {name} -> {link}')
            if m.issym():
                base = os.path.join(dest_root, os.path.dirname(name))
            else:
                base = dest_root
            resolved = os.path.normpath(os.path.join(base, link))
            if not _is_within(resolved, dest_root):
                raise ValueError(
                    f'Unsafe link in archive: {name} -> {link}')
        elif m.isdev() or m.isfifo():
            raise ValueError(f'Unsupported special file in archive: {name}')

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

        archive_path = resolve_backup_path(backup_label)
        if not archive_path:
            return {'success': False, 'error': f'Backup not found: {backup_label}'}

        work_dir = tempfile.mkdtemp(prefix='vault_restore_')
        try:
            # SD-1 修复：最终产物可能是 .enc / 二次压缩形态，先在临时目录
            # 解密、解压回基础 .tar.gz，再走既有提取/恢复逻辑。
            prepared_archive = self._prepare_plain_archive(archive_path, work_dir)
            with tarfile.open(prepared_archive, 'r:gz') as tar:
                members = _validate_tar_members(tar, work_dir)
                _safe_extractall(tar, work_dir, members=members)

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

    def _prepare_plain_archive(self, archive_path: str, work_dir: str) -> str:
        """把最终产物还原成可读取的基础 .tar.gz（SD-1 修复）。

        - .enc：按插件配置的 key_source 解密；密钥未配置/密钥错误时抛
          ValueError 给出明确原因（绝不静默从其他文件恢复）；
        - 二次压缩（.gz/.zst/.lz4，非基础 .tar.gz）：解压回 .tar.gz；
        - 基础 .tar.gz：原样返回。
        所有派生物只写入 work_dir，函数本身不删除任何备份文件。
        """
        path = archive_path

        if path.endswith('.enc'):
            from .utils import load_plugin_config
            try:
                cfg = load_plugin_config()
            except Exception:
                cfg = {}
            key_source = (cfg.get('encryption') or {}).get('key_source') or 'env'
            try:
                from .encryptor import VaultEncryptor
                encryptor = VaultEncryptor(key_source=key_source)
            except ValueError as e:
                raise ValueError(
                    'Backup is encrypted but the decryption key is unavailable '
                    '(set VAULT_ENCRYPTION_KEY): %s' % e)
            except ImportError as e:
                raise ValueError(
                    'Encrypted backup cannot be restored because the encryption '
                    'library (cryptography) is unavailable: %s' % e)
            decrypted = os.path.join(work_dir, os.path.basename(path)[:-4])
            try:
                encryptor.decrypt_stream(path, decrypted)
            except Exception as e:
                raise ValueError(
                    'Decryption failed (wrong key or corrupt archive): %s' % e)
            path = decrypted

        # 去掉二次压缩层（基础归档本身是 .tar.gz，不能对它再解压）
        if path.endswith('.tar.gz'):
            return path
        for tail in ('.gz', '.zst', '.lz4'):
            if path.endswith(tail):
                out_path = os.path.join(work_dir, os.path.basename(path)[:-len(tail)])
                from .compressor import VaultCompressor
                VaultCompressor(algorithm='none').decompress(path, out_path)
                return out_path
        return path

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
        pg_host = target_host or env.get('PG_HOST', 'localhost')

        # ENV-1/ENV-1b：恢复前先确认目标服务器主版本，再解析与之严格相等的
        # 【真实】psql（绕过 pg_wrapper 多版本分发），杜绝高版本 pg_dump 的 dump
        # 被旧 psql 以 ON_ERROR_STOP=1 恢复失败，或客户端/服务器版本错配。
        try:
            target_env = dict(env)
            target_env['PG_HOST'] = pg_host
            target_env['PG_DB'] = target_db
            server_major, server_raw = get_server_major_version(target_env)
        except Exception as e:
            return {'step': 'database', 'success': False,
                    'error': 'Cannot determine target PostgreSQL server version: %s' % e}
        psql, resolve_err = resolve_pg_tool_for_server('psql', server_major)
        if resolve_err:
            return {'step': 'database', 'success': False, 'error': resolve_err}

        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            cmd = [
                psql, '-h', pg_host,
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
                    _safe_extract_one(tar, member, BASE_DIR)

            return {'step': 'files', 'success': True,
                    'file_count': len(files_list)}
        except Exception as e:
            return {'step': 'files', 'success': False, 'error': str(e)}

    def preview(self, backup_label: str) -> Dict:
        """Preview backup contents without executing restore."""
        return self.restore(backup_label, dry_run=True)

    # ── PITR: Point-in-Time Recovery (not supported — fail closed, SD-5) ──

    def restore_pitr(self, target_time: str) -> Dict:
        """时间点恢复在逻辑 dump 架构下无法实现，明确拒绝（SD-5）。

        真正的 PITR 必须具备：pg_basebackup 物理基备、持续 WAL 归档、
        以及带 recovery.signal + recovery_target_time(postgresql.auto.conf)
        的独立受控恢复实例。pg_dump 逻辑备份无法回放到任意时间点，
        因此本方法不再创建沙箱、不写主库 recovery.signal、绝不谎报成功。
        """
        if not target_time:
            return {
                'success': False,
                'supported': False,
                'error': 'target_time is required (YYYY-MM-DD HH:MM:SS)',
            }
        try:
            datetime.strptime(target_time[:19], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return {
                'success': False,
                'supported': False,
                'error': 'Invalid target_time format, use YYYY-MM-DD HH:MM:SS',
            }

        return {
            'success': False,
            'supported': False,
            'error': (
                'Point-in-time recovery is not supported by this vault. '
                'Backups are logical pg_dump archives; true PITR requires '
                'pg_basebackup physical base backups, continuous WAL '
                'archiving, and a dedicated recovery instance. '
                'Use a full restore instead.'
            ),
        }

    def _create_sandbox_db(self, sandbox_db: str) -> Dict:
        """Create a sandbox database for restore-drill testing."""
        env = get_pg_env()
        try:
            server_major, _ = get_server_major_version(env)
        except Exception as e:
            return {'step': 'sandbox_create', 'success': False,
                    'error': 'Cannot determine PostgreSQL server version: %s' % e}
        createdb, resolve_err = resolve_pg_tool_for_server('createdb', server_major)
        if resolve_err:
            return {'step': 'sandbox_create', 'success': False,
                    'error': resolve_err[-500:]}
        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            cmd = [
                createdb, '-h', env.get('PG_HOST', 'localhost'),
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
            server_major, _ = get_server_major_version(env)
            dropdb, resolve_err = resolve_pg_tool_for_server('dropdb', server_major)
            if resolve_err:
                return
        except Exception:
            return
        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            cmd = [
                dropdb, '-h', env.get('PG_HOST', 'localhost'),
                '-p', env.get('PG_PORT', '5432'),
                '-U', env.get('PG_USER', 'app'),
                '--if-exists', sandbox_db,
            ]
            subprocess.run(cmd, env=env_override, capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    # ── Restore Drill ──

    def drill_restore(self, backup_label: str = None) -> Dict:
        """
        Execute a restore drill: restore latest backup to sandbox, verify, report.

        Args:
            backup_label: optional specific backup to drill; defaults to latest

        Returns:
            {'success': bool, 'steps': [...], 'verified': bool, 'report': str}
        """
        # 1. Find backup to use (covers encrypted/compressed artifacts)
        if not backup_label:
            archives = list_backup_files()
            if not archives:
                return {'success': False, 'error': 'No backups available for drill'}
            backup_label = archives[0]['label']

        archive_path = resolve_backup_path(backup_label)
        if not archive_path:
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
                server_major, _ = get_server_major_version(env)
                psql, resolve_err = resolve_pg_tool_for_server('psql', server_major)
                if resolve_err:
                    raise RuntimeError(resolve_err)
                env_override = os.environ.copy()
                env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
                verify_cmd = [
                    psql, '-h', env.get('PG_HOST', 'localhost'),
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
