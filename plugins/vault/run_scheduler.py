#!/usr/bin/env python3
"""
Vault Scheduler Runner — Called by system cron every minute.

Usage:
  */1 * * * * cd /path/to/verorun && python plugins/vault/run_scheduler.py

Or via systemd timer:
  [Unit]
  Description=VeroRun Vault Scheduled Backup
  [Timer]
  OnCalendar=*:0/1
  [Install]
  WantedBy=timers.target
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from plugins.vault.services.scheduler import VaultScheduler
from plugins.vault.services.notifier import VaultNotifier


def _acquire_singleton_lock():
    """获取跨进程排他锁（F-C）。

    执行器每分钟由 cron/systemd 拉起一个独立进程；上一个进程若仍在执行
    慢备份，新进程不得并发处理同一批到期计划。使用 stdlib 实现：
    - POSIX: fcntl.flock(LOCK_EX | LOCK_NB)
    - Windows: msvcrt.locking(LK_NBLCK)
    拿不到锁立即返回 None，由调用方安静退出。锁文件为运行时产物
    data/vault/.scheduler.lock（进程退出后由 OS 自动释放，非源码资产）。
    返回保持打开的文件对象（锁随其生命周期持有），失败返回 None。
    """
    backup_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '..', '..', 'data', 'vault')
    os.makedirs(backup_dir, exist_ok=True)
    lock_path = os.path.join(backup_dir, '.scheduler.lock')
    fp = open(lock_path, 'a+')
    fp.seek(0)

    # 必须先抢锁再写文件：Windows 下第二个进程若先 truncate/写已被持有的
    # 锁区域会直接 PermissionError，而不是安静退出。
    try:
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fp.close()
                return None
        else:
            import fcntl
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fp.close()
                return None
    except Exception:
        fp.close()
        raise

    fp.seek(0)
    fp.truncate()
    fp.write(str(os.getpid()))
    fp.flush()
    fp.seek(0)
    return fp


def main():
    lock_fp = _acquire_singleton_lock()
    if lock_fp is None:
        print('[Vault] Another scheduler instance is running; this tick exits.')
        return

    scheduler = VaultScheduler()
    notifier = VaultNotifier()

    results = scheduler.run_all_due()

    for result in results:
        if result['success']:
            print(f"[Vault] Schedule {result['schedule_id']}: OK")
        else:
            error_msg = result.get('error', 'Unknown error')
            print(f"[Vault] Schedule {result['schedule_id']}: FAILED - {error_msg}")
            notifier.send(
                event='backup.schedule.failed',
                message=f'Scheduled backup {result["schedule_id"]} failed: {error_msg}',
                level='error',
                details=result,
            )


if __name__ == '__main__':
    main()
