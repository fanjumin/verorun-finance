#!/usr/bin/env python3
"""
Vault Plugin — 数据备份与恢复
==============================
全站数据保险库：数据库备份(PG dump)、文件归档(tar)、远程存储(S3/OSS)、定时自动备份。
"""

import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin


class VaultPlugin(BasePlugin):
    name = 'vault'
    version = '2.1.1'
    description = 'Data vault — full/incremental backup, AES-256-GCM encryption, scheduled backups, audit logging, multi-target storage'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时创建备份目录"""
        import os as _os
        _backup_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', 'data', 'vault')
        _os.makedirs(_backup_dir, exist_ok=True)
        return True

    def on_enable(self, registry):
        """启用时初始化备份目录 + 注册定时备份 + 确保数据表存在"""
        import os as _os
        _backup_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', 'data', 'vault')
        _os.makedirs(_backup_dir, exist_ok=True)
        print('[Vault] Backup directory ready')

        # Ensure vault_* tables exist (idempotent migration, no-op if already applied)
        try:
            from .services.utils import ensure_schema
            ensure_schema()
        except Exception as e:
            print('[Vault] on_enable schema ensure skipped: %s' % e)

        # Register scheduled backup with orchestrator
        self._seed_schedule()
        return True

    def register_routes(self):
        from .routes import vault_bp
        return [vault_bp]

    def on_disable(self, registry):
        pass

    def _seed_schedule(self):
        """Register scheduled backup job in orchestrator cron_jobs table."""
        try:
            from orchestrator.models import get_db as orch_db
        except ImportError:
            print('[Vault] orchestrator not available, skipping schedule seeding')
            return

        import json
        name = 'Vault — Daily Backup'
        try:
            with orch_db() as conn:
                # orchestrator 的 get_db() 返回裸 cursor，其 execute() 返回 None，
                # 必须先 execute 再 fetchone，不能链式调用。
                conn.execute(
                    'SELECT id FROM cron_jobs WHERE name=%s', (name,)
                )
                existing = conn.fetchone()
                if existing:
                    print('[Vault] Backup schedule already registered')
                    return

                target_config = json.dumps({
                    'url': 'http://127.0.0.1:8084/admin/vault/api/create',
                    'method': 'POST',
                    'headers': {
                        'Content-Type': 'application/json',
                        'X-Internal-Secret': os.environ.get('HEALTH_SECRET', '') or None,
                    },
                    'body': {'trigger_type': 'scheduled'},
                }, ensure_ascii=False)

                conn.execute("""
                    INSERT INTO cron_jobs
                        (name, description, job_type, cron_expr, natural_expr,
                         interval_seconds, is_active, target_type, target_config,
                         priority, max_retries, retry_delay, max_runs)
                    VALUES (%s,%s,%s,%s,%s,%s,1,'api',%s,%s,2,60,0)
                """, (
                    name,
                    'Daily database and files backup at 03:00 UTC',
                    'cron',
                    '0 3 * * *',
                    '',
                    0,
                    target_config,
                    'low',
                ))
                print('[Vault] Daily backup schedule registered (03:00 UTC)')
        except Exception as e:
            print(f'[Vault] Failed to register backup schedule: {e}')

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读 vault schema，幂等）。"""
        import psycopg2.extras
        stats = {'total_backups': 0, 'success_backups': 0, 'total_schedules': 0}
        try:
            from .services.utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                cur.execute('SELECT COUNT(*) AS c FROM vault_backups')
                total = cur.fetchone()
                cur.execute("SELECT COUNT(*) AS c FROM vault_backups WHERE status='success'")
                done = cur.fetchone()
                cur.execute('SELECT COUNT(*) AS c FROM vault_schedules')
                sched = cur.fetchone()
                stats['total_backups'] = int(total['c']) if total else 0
                stats['success_backups'] = int(done['c']) if done else 0
                stats['total_schedules'] = int(sched['c']) if sched else 0
            finally:
                cur.close()
                conn.close()
        except Exception as e:
            print('[Vault] get_dashboard_stats failed: %s' % e)
        return stats

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 vault schema（标准 §12.5 卸载零残留）。

        仅删除数据库中的备份元数据表；文件系统备份目录 data/vault 保留。
        """
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS vault CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            print('[Vault] vault schema dropped (backup files preserved)')
        except Exception as e:
            print('[Vault] on_uninstall cleanup failed: %s' % e)
        return True
