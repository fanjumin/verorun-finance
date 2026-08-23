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


def main():
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
