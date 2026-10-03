#!/usr/bin/env python3
"""
Vault Backup Engine — Full / Incremental / Differential backup entry point.

Supports:
  - Full backup: pg_dump + tar.gz of database and files
  - Incremental backup: WAL archive collection since last backup
  - Differential backup: changes since last full backup
  - Selective backup: specific tables, plugins, directories
"""

import os
import re
import json
import subprocess
import tarfile
import hashlib
import shutil
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from .utils import get_pg_env, BASE_DIR, BACKUP_DIR


def resolve_pg_tool(tool_name: str = 'pg_dump') -> str:
    """定位 PG 客户端工具（pg_dump / psql / createdb / dropdb）。

    Windows native 便携部署下 PG bin 目录不在 PATH（裸调报 WinError 2），
    而 electron 本地核心会注入 PG_BIN=<便携 PG 的 bin 目录>（见 nativeCoreEnv）。
    解析优先级：<TOOL>_PATH（显式文件路径，如 PG_DUMP_PATH/PSQL_PATH）
              → PG_BIN（bin 目录）→ PATH 搜索 → 原样兜底。
    """
    exe = tool_name + ('.exe' if os.name == 'nt' else '')
    upper = tool_name.upper().replace('-', '_')
    for key in (f'{upper}_PATH', 'PG_BIN'):
        val = (os.environ.get(key) or '').strip()
        if not val:
            continue
        cand = val if os.path.basename(val).startswith(tool_name) else os.path.join(val, exe)
        if os.path.isfile(cand):
            return cand
    return shutil.which(tool_name) or tool_name


def _resolve_pg_dump() -> str:
    """兼容旧调用方：pg_dump 路径解析（委托通用解析器）。"""
    return resolve_pg_tool('pg_dump')


def _is_pg_wrapper(tool_path: str) -> bool:
    """检测 Debian/Ubuntu 的 pg_wrapper 多版本分发器。

    /usr/bin/pg_dump、psql 常是指向 /usr/share/postgresql-common/pg_wrapper
    的符号链接：裸 `--version` 报默认集群版本，带 -h 真实连接时却执行已安装的
    最高版本客户端，二者可能不是同一个二进制。realpath 后名为 pg_wrapper 即判定。
    """
    try:
        return os.path.basename(os.path.realpath(tool_path)) == 'pg_wrapper'
    except Exception:
        return False


def _versioned_pg_tool_dirs(server_major: int) -> List[str]:
    """按服务器主版本定位客户端 bin 目录（仅类 Unix 布局；Windows 走 PG_BIN）。"""
    if os.name == 'nt':
        return []
    return [
        f'/usr/lib/postgresql/{server_major}/bin',   # Debian/Ubuntu PGDG
        f'/usr/pgsql-{server_major}/bin',            # RHEL/CentOS/Rocky PGDG
        '/usr/local/pgsql/bin',                      # 源码编译布局（靠实测版本把关）
    ]


def _pg_tool_resolution_error(tool_name: str, server_major: int,
                              attempted: List[Tuple[str, str]],
                              explicit_key: Optional[str] = None,
                              explicit_path: Optional[str] = None) -> str:
    """构造 fail-closed 的可操作错误信息。"""
    head = ('No PostgreSQL %s client matching server major version v%s was found.'
            % (tool_name, server_major))
    if explicit_key and explicit_path:
        head += (' %s=%s points at an incompatible binary; refusing to silently '
                 'substitute a different version.' % (explicit_key, explicit_path))
    lines = [head]
    if attempted:
        lines.append('Checked candidates:')
        for path, note in attempted:
            lines.append('  - %s (%s)' % (path, note))
    lines.append('Install the matching postgresql-client-%s package, or set '
                 '%s_PATH / PG_BIN to the PostgreSQL v%s bin directory.'
                 % (server_major, tool_name.upper().replace('-', '_'), server_major))
    return '\n'.join(lines)


def resolve_pg_tool_for_server(tool_name: str,
                               server_major: int
                               ) -> Tuple[Optional[str], Optional[str]]:
    """解析与服务器主版本【严格相等】的真实客户端二进制。

    ENV-1b：resolve_pg_tool()+`--version` 的探测会被 pg_wrapper 欺骗（裸探测报
    默认集群版本、带 -h 实际执行最高版本）。本函数直接定位磁盘上的真实二进制并
    实测其版本，成功返回 (path, None)；找不到匹配版本时 fail-closed 返回
    (None, error_message)，绝不放任产出不可恢复的 dump。

    选择顺序：
      1. 显式 <TOOL>_PATH / PG_BIN：真实二进制且版本匹配 → 用；真实二进制但
         版本不符 → fail-closed（尊重显式固定，不偷偷替换）；若指向 pg_wrapper
         （不可信）→ 记录并转入自动发现；
      2. 版本化目录 /usr/lib/postgresql/<major>/bin 等，逐个实测版本；
      3. PATH 兜底：仅接受 realpath 非 pg_wrapper 且版本实测相等者。
    """
    exe = tool_name + ('.exe' if os.name == 'nt' else '')
    upper = tool_name.upper().replace('-', '_')
    attempted: List[Tuple[str, str]] = []
    seen = set()

    def _try_real_binary(path: str) -> Optional[str]:
        """真实（非 wrapper）且主版本匹配则返回路径；否则记录后返回 None。"""
        if not path or not os.path.isfile(path):
            return None
        real = os.path.realpath(path)
        if real in seen:
            return None
        seen.add(real)
        if _is_pg_wrapper(path):
            attempted.append((path, 'pg_wrapper multiplexer, version varies by invocation'))
            return None
        major, raw = get_tool_major_version(path)
        if major == server_major:
            return path
        attempted.append((path, 'client v%s != server v%s (%s)'
                          % (major, server_major, (raw or '').strip()[:80])))
        return None

    # 1) 显式配置
    for key in (f'{upper}_PATH', 'PG_BIN'):
        val = (os.environ.get(key) or '').strip()
        if not val:
            continue
        cand = val if os.path.basename(val).startswith(tool_name) else os.path.join(val, exe)
        if not os.path.isfile(cand):
            continue
        matched = _try_real_binary(cand)
        if matched:
            return matched, None
        # 显式指向真实但版本不符的二进制 → fail-closed；指向 wrapper 则继续自动发现
        if not _is_pg_wrapper(cand):
            return None, _pg_tool_resolution_error(
                tool_name, server_major, attempted,
                explicit_key=key, explicit_path=cand)

    # 2) 版本化目录
    for directory in _versioned_pg_tool_dirs(server_major):
        matched = _try_real_binary(os.path.join(directory, exe))
        if matched:
            return matched, None

    # 3) PATH 兜底（拒绝 wrapper）
    which = shutil.which(tool_name)
    matched = _try_real_binary(which) if which else None
    if matched:
        return matched, None

    return None, _pg_tool_resolution_error(tool_name, server_major, attempted)



def parse_pg_major_version(text: str) -> Optional[int]:
    """从版本文本解析 PG 主版本号（PG10+ 约定：主版本为第一个整数）。

    接受 '14.24'、'17beta1'、'pg_dump (PostgreSQL) 14.24 (Ubuntu ...)' 等。
    无法解析返回 None。
    """
    if not text:
        return None
    m = re.search(r'(\d+)(?:\.\d+)?', str(text))
    return int(m.group(1)) if m else None


def get_tool_major_version(tool_path: str) -> Tuple[Optional[int], str]:
    """执行 `<tool> --version`，返回 (主版本号, 原始输出)。

    工具不可执行或返回非零：(None, 错误描述)。带 15s timeout，fail-closed。
    """
    try:
        proc = subprocess.run([tool_path, '--version'], capture_output=True,
                              text=True, timeout=15, errors='replace')
    except Exception as e:
        return None, 'cannot execute %s: %s' % (tool_path, e)
    raw = (proc.stdout or proc.stderr or '').strip()
    if proc.returncode != 0:
        return None, raw or ('%s --version exited %s' % (tool_path, proc.returncode))
    return parse_pg_major_version(raw), raw


def get_server_major_version(env: Dict[str, str]) -> Tuple[int, str]:
    """连接目标实例执行 SHOW server_version，返回 (主版本号, 原始值)。

    connect_timeout=10s；连接失败或版本无法解析时抛异常（fail-closed，
    绝不允许在未知服务器版本的情况下产备份/恢复）。
    """
    import psycopg2
    conn = psycopg2.connect(
        host=env.get('PG_HOST', 'localhost'),
        port=env.get('PG_PORT', '5432'),
        user=env.get('PG_USER', 'app'),
        password=env.get('PG_PASSWORD', ''),
        dbname=env.get('PG_DB', 'appdb'),
        connect_timeout=10,
    )
    try:
        cur = conn.cursor()
        cur.execute('SHOW server_version')
        raw = str(cur.fetchone()[0])
        cur.close()
    finally:
        conn.close()
    major = parse_pg_major_version(raw)
    if major is None:
        raise RuntimeError('unparseable server_version: %r' % raw)
    return major, raw


def pg_version_mismatch_error(tool_name: str,
                              client_major: Optional[int], client_raw: str,
                              server_major: int, server_raw: str) -> Optional[str]:
    """纯函数：判定客户端工具与服务器主版本是否匹配，返回错误信息或 None。

    ENV-1 铁律：主版本必须严格相等，任一方向不一致都拒绝（pg_dump 高版本
    对低版本服务器产出的 dump 含旧客户端无法识别的 SET 项，恢复必失败）。
    """
    if client_major is None:
        return ('Cannot determine %s client version (%s). Point PG_BIN or '
                '%s_PATH to a matching PostgreSQL %s bin directory.'
                % (tool_name, client_raw or 'unknown',
                   tool_name.upper().replace('-', '_'), server_major))
    if client_major != server_major:
        return ('PostgreSQL major version mismatch: %s client is v%s (%s) but '
                'target server is v%s (%s). Refusing to proceed because a dump '
                'produced by a mismatched client may be unrestorable. Point '
                'PG_BIN or %s_PATH to the PostgreSQL v%s bin directory.'
                % (tool_name, client_major, client_raw.strip(),
                   server_major, server_raw.strip(),
                   tool_name.upper().replace('-', '_'), server_major))
    return None


class BackupEngine:
    """Unified backup engine supporting full, incremental, and differential modes."""

    def __init__(self, backup_root: str = None):
        self.backup_root = backup_root or BACKUP_DIR
        os.makedirs(self.backup_root, exist_ok=True)

    def create_backup(self, backup_type: str = 'full',
                      base_label: str = None,
                      scope: Dict = None) -> Dict:
        """
        Execute a backup.

        Args:
            backup_type: 'full' | 'incremental' | 'differential'
            base_label: base backup label for incremental/differential
            scope: selective backup scope, e.g. {'tables': ['users','orders'], 'plugins': ['vault']}

        Returns:
            {'label': str, 'archive': str, 'size_mb': float, 'success': bool, 'error': str|None}
        """
        label = f"vault_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        work_dir = os.path.join(self.backup_root, label)
        os.makedirs(work_dir, exist_ok=True)

        errors = []
        files = []

        # ── 1. Database backup ──
        if not scope or scope.get('include_db', True):
            if backup_type == 'full':
                db_result = self._dump_full(work_dir, label, scope)
            elif backup_type == 'incremental':
                db_result = self._dump_incremental(work_dir, label, base_label, scope)
            else:
                db_result = self._dump_differential(work_dir, label, base_label, scope)
            if db_result:
                files.append(db_result)
            else:
                errors.append('database dump failed')

        # ── 2. Config export ──
        if not scope or scope.get('include_config', True):
            config_result = self._dump_config(work_dir, label)
            if config_result:
                files.append(config_result)
            else:
                errors.append('config export failed')

        # ── 3. File archive ──
        if not scope or scope.get('include_files', True):
            file_result = self._archive_files(work_dir, label, scope)
            if file_result:
                files.append(file_result)
            else:
                errors.append('file archive failed')

        # ── 3. Package + checksum ──
        final_archive = os.path.join(self.backup_root, f'{label}.tar.gz')
        try:
            with tarfile.open(final_archive, 'w:gz') as tar:
                tar.add(work_dir, arcname=label)
            shutil.rmtree(work_dir, ignore_errors=True)

            size_mb = os.path.getsize(final_archive) / (1024 * 1024)
            sha256 = self._compute_sha256(final_archive)

            return {
                'label': label,
                'archive': final_archive,
                'backup_type': backup_type,
                'base_label': base_label,
                'size_mb': round(size_mb, 1),
                'checksum_sha256': sha256,
                'files': files,
                'success': len(errors) == 0,
                'error': '; '.join(errors) if errors else None,
            }
        except Exception as e:
            return {
                'label': label, 'archive': None, 'backup_type': backup_type,
                'success': False, 'error': f'Archive creation failed: {e}',
            }

    # ── Full backup ──
    def _dump_full(self, work_dir: str, label: str, scope: Dict) -> Optional[Dict]:
        """pg_dump complete database export."""
        env = get_pg_env()
        out_file = os.path.join(work_dir, f'{label}_db.sql')
        tables = scope.get('tables') if scope else None

        # ENV-1/ENV-1b：dump 前先确认服务器主版本，再解析与之严格相等的【真实】
        # pg_dump 二进制。Debian/Ubuntu 的 pg_wrapper 裸 --version 报默认集群版本、
        # 带 -h 实际却执行最高版本客户端，必须绕过它定位真实二进制，避免产出
        # "当时成功、日后无法恢复"的静默坏 dump。
        try:
            server_major, server_raw = get_server_major_version(env)
        except Exception as e:
            print(f'[Vault] cannot determine PostgreSQL server version, dump aborted: {e}')
            return None
        pg_dump, resolve_err = resolve_pg_tool_for_server('pg_dump', server_major)
        if resolve_err:
            print(f'[Vault] {resolve_err}')
            return None

        try:
            env_override = os.environ.copy()
            env_override['PGPASSWORD'] = env.get('PG_PASSWORD', '')
            cmd = [
                pg_dump, '-h', env.get('PG_HOST', 'localhost'),
                '-p', env.get('PG_PORT', '5432'),
                '-U', env.get('PG_USER', 'app'),
                '-d', env.get('PG_DB', 'appdb'),
                '--no-owner', '--no-acl', '-f', out_file,
            ]
            if tables:
                for t in tables:
                    cmd.extend(['-t', t])
            # errors='replace'：PYTHONUTF8=1 下 pg_dump 的 GBK 中文 stderr 默认按
            # utf-8 解码会在 _readerthread 内抛 UnicodeDecodeError → communicate
            # 返回 stderr=None → 下方 .strip() 抛 TypeError（根因审计 2026-09-19）。
            proc = subprocess.run(cmd, env=env_override, capture_output=True,
                                  text=True, timeout=600, errors='replace')
            if proc.returncode != 0:
                print(f'[Vault] pg_dump failed (rc={proc.returncode}): '
                      f'{(proc.stderr or "").strip()}')
                return None
            return {'type': 'database', 'path': out_file, 'name': os.path.basename(out_file)}
        except Exception as e:
            print(f'[Vault] pg_dump error: {e}')
            return None

    # ── Incremental backup (WAL-based) ──
    def _dump_incremental(self, work_dir: str, label: str,
                          base_label: str, scope: Dict) -> Optional[Dict]:
        """Collect WAL log files since last backup."""
        pg_env = self._get_pg_env()
        archive_dir = pg_env.get('WAL_ARCHIVE_DIR', '/var/lib/postgresql/wal_archive')
        try:
            base_time = self._get_backup_time(base_label) if base_label else 0
            wal_files = []
            if os.path.isdir(archive_dir):
                for f in sorted(os.listdir(archive_dir)):
                    fpath = os.path.join(archive_dir, f)
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) >= base_time:
                        wal_files.append(fpath)
            if not wal_files:
                print('[Vault] No WAL files found since base backup')
                return {'type': 'wal', 'path': None, 'name': 'wal_empty', 'count': 0}

            wal_archive = os.path.join(work_dir, f'{label}_wal.tar.gz')
            with tarfile.open(wal_archive, 'w:gz') as tar:
                for wf in wal_files:
                    tar.add(wf, arcname=os.path.basename(wf))
            return {
                'type': 'wal', 'path': wal_archive,
                'name': os.path.basename(wal_archive), 'count': len(wal_files),
            }
        except Exception as e:
            print(f'[Vault] WAL backup error: {e}')
            return None

    # ── Differential backup ──
    def _dump_differential(self, work_dir: str, label: str,
                           base_label: str, scope: Dict) -> Optional[Dict]:
        """Changes since last full backup."""
        return self._dump_incremental(work_dir, label, base_label, scope)

    def _archive_files(self, work_dir: str, label: str, scope: Dict) -> Optional[Dict]:
        """Package user files (supports selective plugin/directory scope)."""
        out_file = os.path.join(work_dir, f'{label}_files.tar.gz')
        try:
            with tarfile.open(out_file, 'w:gz') as tar:
                plugins = scope.get('plugins') if scope else None
                dirs = scope.get('directories') if scope else None

                if not plugins and not dirs:
                    for root in ['admin/static', 'main_site/static', 'images']:
                        path = os.path.join(BASE_DIR, root)
                        if os.path.isdir(path):
                            tar.add(path, arcname=root)
                    self._add_plugin_data(tar, None)
                else:
                    if dirs:
                        for d in dirs:
                            path = os.path.join(BASE_DIR, d)
                            if os.path.isdir(path):
                                tar.add(path, arcname=d)
                    self._add_plugin_data(tar, plugins)

            return {'type': 'files', 'path': out_file, 'name': os.path.basename(out_file)}
        except Exception as e:
            print(f'[Vault] File archive error: {e}')
            return None

    def _add_plugin_data(self, tar: tarfile.TarFile, plugin_filter: List[str] = None):
        """Add plugin data/ directories to tar."""
        plugins_dir = os.path.join(BASE_DIR, 'plugins')
        if not os.path.isdir(plugins_dir):
            return
        for name in sorted(os.listdir(plugins_dir)):
            if name.startswith('_'):
                continue
            if plugin_filter and name not in plugin_filter:
                continue
            plugin_path = os.path.join(plugins_dir, name)
            if not os.path.isdir(plugin_path):
                continue
            data_path = os.path.join(plugin_path, 'data')
            if os.path.isdir(data_path):
                tar.add(data_path, arcname=f'plugins/{name}/data')
            json_path = os.path.join(plugin_path, 'plugin.json')
            if os.path.isfile(json_path):
                tar.add(json_path, arcname=f'plugins/{name}/plugin.json')

    def _get_pg_env(self) -> Dict[str, str]:
        """Read .env for PostgreSQL connection info."""
        return get_pg_env()

    @staticmethod
    def _redact_env(env_path: str) -> str:
        """Read .env file, redact sensitive fields."""
        sensitive_keys = {'PG_PASSWORD', 'JWT_SECRET', 'FLASK_SECRET_KEY',
                          'DASHSCOPE_TEXT_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY',
                          'PLUGIN_LICENSE_SECRET', 'CAPTCHA_SECRET_KEY',
                          'DEV_ACCOUNTS_ENCRYPTION_KEY', 'LICENSE_SERVER_SECRET'}
        lines = []
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k = line.split('=', 1)[0]
                    if k in sensitive_keys:
                        lines.append(f'{k}=***REDACTED***')
                    else:
                        lines.append(line)
                else:
                    lines.append(line)
        return '\n'.join(lines)

    def _dump_config(self, work_dir: str, label: str) -> Optional[Dict]:
        """Export system config table + redacted .env to JSON."""
        out_file = os.path.join(work_dir, f'{label}_config.json')
        try:
            from plugins._base.db import get_raw_connection
            conn = get_raw_connection()
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM system_config ORDER BY key")
            rows = cur.fetchall()
            config_data = {row[0]: row[1] for row in rows}
            cur.close()
            conn.close()
        except Exception as e:
            print(f'[Vault] system_config export failed: {e}')
            config_data = {}

        env_path = os.path.join(BASE_DIR, '.env')
        if os.path.exists(env_path):
            config_data['_.env._contents'] = self._redact_env(env_path)

        try:
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            print(f'[Vault] Config exported: {out_file}')
            return {'type': 'config', 'path': out_file, 'name': os.path.basename(out_file)}
        except Exception as e:
            print(f'[Vault] Config export error: {e}')
            return None

    def _get_backup_time(self, label: str) -> float:
        """Get creation timestamp of a backup."""
        archive = os.path.join(self.backup_root, f'{label}.tar.gz')
        if os.path.isfile(archive):
            return os.path.getmtime(archive)
        return 0.0

    @staticmethod
    def _compute_sha256(file_path: str) -> str:
        sha = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha.update(chunk)
        return sha.hexdigest()


# ── Convenience factory functions ──
def create_full_backup(scope: Dict = None) -> Dict:
    return BackupEngine().create_backup('full', scope=scope)


def create_incremental_backup(base_label: str, scope: Dict = None) -> Dict:
    return BackupEngine().create_backup('incremental', base_label=base_label, scope=scope)


def finalize_artifact(archive_path: str, cfg: Dict) -> Dict:
    """对备份原件执行 压缩 → 加密 后置管线（SD-1 修复）。

    关键约定：
      - 每一级产物落盘后立即删除上一级中间文件，最终磁盘只保留最终产物，
        杜绝"加密备份仍残留明文原件"；
      - 返回值中的 size/checksum 一律针对**最终产物**重算，调用方必须将
        archive 回写主结果，使上传/落库/列表/恢复指向同一个磁盘文件；
      - 压缩仅在 cfg['compression']['enabled'] 显式为 True 时执行——基础归档
        本身已是 gzip tar，默认不重复压缩；
      - 加密在 cfg['encryption']['enabled'] 为 True 时执行；密钥未配置
        （ValueError）时告警并保留原文件，兼容历史降级行为。

    Args:
        archive_path: create_backup() 产出的原始 vault_*.tar.gz 路径
        cfg: 插件配置（plugin_registry.config），读取 compression/encryption 段

    Returns:
        {'archive': str, 'encrypted': bool, 'encryption': str,
         'compressed': bool, 'size_mb': float,
         'compressed_size_mb': float|None, 'checksum_sha256': str}
    """
    result = {
        'archive': archive_path,
        'encrypted': False,
        'encryption': 'none',
        'compressed': False,
        'size_mb': round(os.path.getsize(archive_path) / (1024 * 1024), 1),
        'compressed_size_mb': None,
        'checksum_sha256': None,
    }
    current = archive_path

    # 1. Compression（显式启用才执行）
    comp_cfg = cfg.get('compression') or {}
    if comp_cfg.get('enabled') is True:
        algorithm = comp_cfg.get('algorithm', 'gzip') or 'gzip'
        try:
            level = int(comp_cfg.get('level', 6) or 6)
        except (TypeError, ValueError):
            level = 6
        from .compressor import VaultCompressor
        compressor = VaultCompressor(algorithm=algorithm, level=level)
        compressed_path = compressor.compress(current)
        if compressed_path != current and os.path.exists(compressed_path):
            if os.path.exists(current):
                os.remove(current)
            current = compressed_path
            result['compressed'] = True

    # 2. Encryption（启用且密钥可用时执行）
    enc_cfg = cfg.get('encryption') or {}
    if enc_cfg.get('enabled') is True:
        try:
            from .encryptor import VaultEncryptor
            key_source = enc_cfg.get('key_source', 'env') or 'env'
            encryptor = VaultEncryptor(key_source=key_source)
            encrypted_path = encryptor.encrypt_stream(current)
            if encrypted_path != current and os.path.exists(encrypted_path):
                if os.path.exists(current):
                    os.remove(current)
                current = encrypted_path
                result['encrypted'] = True
                result['encryption'] = 'aes256-gcm'
        except ValueError as e:
            # 密钥未配置：降级为明文并告警（与历史行为一致）
            print(f'[Vault] Encryption skipped (key not configured): {e}')
        except Exception as e:
            print(f'[Vault] Encryption skipped: {e}')

    # 3. 针对最终产物重算体积与校验和
    result['archive'] = current
    result['size_mb'] = round(os.path.getsize(current) / (1024 * 1024), 1)
    if result['compressed']:
        result['compressed_size_mb'] = result['size_mb']
    result['checksum_sha256'] = BackupEngine._compute_sha256(current)
    return result
