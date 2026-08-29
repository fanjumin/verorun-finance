#!/usr/bin/env python3
"""
Vault Dumper — 数据库导出 + 文件归档
=====================================
- pg_dump: 完整 PostgreSQL 导出 (所有 schema)
- tar: 打包用户上传文件 + 配置文件
"""

import os
import sys
import json
import subprocess
import tarfile
import tempfile
import shutil
from datetime import datetime

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
sys.path.insert(0, os.path.join(BASE_DIR, 'auth-center'))


def get_pg_env():
    """读取 .env 获取 PostgreSQL 连接信息."""
    env = {}
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def dump_database(output_dir: str, label: str) -> str:
    """
    执行 pg_dump 导出完整数据库。
    返回导出的 .sql 文件路径，失败返回 None。
    """
    env = get_pg_env()
    pg_host = env.get('PG_HOST', 'localhost')
    pg_port = env.get('PG_PORT', '5432')
    pg_db = env.get('PG_DB', 'appdb')
    pg_user = env.get('PG_USER', 'app')
    pg_pass = env.get('PG_PASSWORD', '')

    out_file = os.path.join(output_dir, f'{label}_db.sql')
    try:
        env_override = os.environ.copy()
        env_override['PGPASSWORD'] = pg_pass
        cmd = [
            'pg_dump',
            '-h', pg_host,
            '-p', pg_port,
            '-U', pg_user,
            '-d', pg_db,
            '--no-owner',
            '--no-acl',
            '-f', out_file,
        ]
        proc = subprocess.run(cmd, env=env_override, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            print(f'[Vault] pg_dump failed: {proc.stderr.strip()}')
            return None
        size_mb = os.path.getsize(out_file) / (1024 * 1024)
        print(f'[Vault] Database dumped: {out_file} ({size_mb:.1f} MB)')
        return out_file
    except Exception as e:
        print(f'[Vault] pg_dump error: {e}')
        return None


def dump_config(output_dir: str, label: str) -> str:
    """
    导出系统配置为 JSON 文件。
    - system_config 表全部 KEY-VALUE
    - .env 文件副本（敏感字段脱敏）
    """
    out_file = os.path.join(output_dir, f'{label}_config.json')
    try:
        from plugins._base.db import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM system_config ORDER BY key")
        rows = cur.fetchall()
        config_data = {row[0]: row[1] for row in rows}
        cur.close()
        conn.close()

        # Add .env info (redact secrets)
        env_path = os.path.join(BASE_DIR, '.env')
        if os.path.exists(env_path):
            config_data['_.env._contents'] = _redact_env(env_path)

        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        print(f'[Vault] Config exported: {out_file}')
        return out_file
    except Exception as e:
        print(f'[Vault] Config export error: {e}')
        return None


def _redact_env(env_path: str) -> str:
    """读 .env 文件，脱敏敏感字段."""
    sensitive_keys = {'PG_PASSWORD', 'JWT_SECRET', 'FLASK_SECRET_KEY', 'DASHSCOPE_TEXT_KEY',
                      'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'PLUGIN_LICENSE_SECRET',
                      'CAPTCHA_SECRET_KEY', 'DEV_ACCOUNTS_ENCRYPTION_KEY', 'LICENSE_SERVER_SECRET'}
    lines = []
    with open(env_path, 'r') as f:
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


def archive_files(output_dir: str, label: str) -> str:
    """
    打包用户文件目录为 tar.gz。
    包含: static/ 目录、插件 data/ 目录、plugin.json 文件。
    """
    out_file = os.path.join(output_dir, f'{label}_files.tar.gz')
    try:
        with tarfile.open(out_file, 'w:gz') as tar:
            # static/ directories
            for root in ['admin/static', 'main_site/static', 'images']:
                path = os.path.join(BASE_DIR, root)
                if os.path.isdir(path):
                    tar.add(path, arcname=root)

            # plugin data/ files (*.db, *.json)
            plugins_dir = os.path.join(BASE_DIR, 'plugins')
            for plugin_name in os.listdir(plugins_dir):
                plugin_path = os.path.join(plugins_dir, plugin_name)
                if not os.path.isdir(plugin_path) or plugin_name.startswith('_'):
                    continue
                data_path = os.path.join(plugin_path, 'data')
                if os.path.isdir(data_path):
                    tar.add(data_path, arcname=f'plugins/{plugin_name}/data')
                json_path = os.path.join(plugin_path, 'plugin.json')
                if os.path.isfile(json_path):
                    tar.add(json_path, arcname=f'plugins/{plugin_name}/plugin.json')

        size_mb = os.path.getsize(out_file) / (1024 * 1024)
        print(f'[Vault] Files archived: {out_file} ({size_mb:.1f} MB)')
        return out_file
    except Exception as e:
        print(f'[Vault] File archive error: {e}')
        return None


def create_full_backup() -> dict:
    """
    执行完整备份：数据库 + 配置 + 文件。
    返回 {'label': str, 'files': [...], 'success': bool, 'error': str|None}
    """
    label = datetime.utcnow().strftime('vault_%Y%m%d_%H%M%S')
    backup_root = os.path.join(BASE_DIR, 'data', 'vault')
    work_dir = os.path.join(backup_root, label)
    os.makedirs(work_dir, exist_ok=True)

    files = []
    errors = []

    # 1. Database dump
    db_file = dump_database(work_dir, label)
    if db_file:
        files.append({'type': 'database', 'path': db_file, 'name': os.path.basename(db_file)})
    else:
        errors.append('database dump failed')

    # 2. Config export
    cfg_file = dump_config(work_dir, label)
    if cfg_file:
        files.append({'type': 'config', 'path': cfg_file, 'name': os.path.basename(cfg_file)})
    else:
        errors.append('config export failed')

    # 3. File archive
    arc_file = archive_files(work_dir, label)
    if arc_file:
        files.append({'type': 'files', 'path': arc_file, 'name': os.path.basename(arc_file)})
    else:
        errors.append('file archive failed')

    # 4. Create final tar.gz
    final_archive = os.path.join(backup_root, f'{label}.tar.gz')
    try:
        with tarfile.open(final_archive, 'w:gz') as tar:
            tar.add(work_dir, arcname=label)
        # Clean up work directory
        shutil.rmtree(work_dir)
        size_mb = os.path.getsize(final_archive) / (1024 * 1024)
        print(f'[Vault] Backup complete: {final_archive} ({size_mb:.1f} MB)')
        return {
            'label': label,
            'archive': final_archive,
            'archive_name': f'{label}.tar.gz',
            'size_mb': round(size_mb, 1),
            'files': files,
            'success': len(errors) == 0,
            'error': '; '.join(errors) if errors else None,
        }
    except Exception as e:
        return {
            'label': label,
            'archive': None,
            'success': False,
            'error': f'Final archive creation failed: {e}',
        }
