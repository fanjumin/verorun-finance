#!/usr/bin/env python3
"""
Vault Notifier — Multi-channel alert notification.

Supports: Email / Webhook / Feishu / DingTalk / WeCom
"""

import json
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, List

from .utils import decrypt_config_secrets


class VaultNotifier:
    """Unified notification dispatcher."""

    def __init__(self):
        self._channels = self._load_channels()

    def _load_channels(self) -> List[Dict]:
        """Load enabled notification channels from plugin config."""
        channels = []
        try:
            from .utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT config FROM plugin_registry WHERE identifier = 'vault'"
            )
            row = cur.fetchone()
            cur.close()
            conn.close()

            if row and row[0]:
                cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                notify = cfg.get('notifications', {})
                if notify.get('email', {}).get('enabled'):
                    channels.append({'type': 'email', 'config': decrypt_config_secrets(notify['email'])})
                if notify.get('webhook', {}).get('enabled'):
                    channels.append({'type': 'webhook', 'config': decrypt_config_secrets(notify['webhook'])})
                if notify.get('feishu', {}).get('enabled'):
                    channels.append({'type': 'feishu', 'config': decrypt_config_secrets(notify['feishu'])})
                if notify.get('dingtalk', {}).get('enabled'):
                    channels.append({'type': 'dingtalk', 'config': decrypt_config_secrets(notify['dingtalk'])})
        except Exception as e:
            print(f'[Vault] Failed to load notification channels: {e}')

        return channels

    def send(self, event: str, message: str, level: str = 'info',
             details: Dict = None) -> List[Dict]:
        """
        Send notification to all enabled channels.

        Args:
            event: event type (backup.success, backup.failed, storage.low, health.warning)
            message: notification message
            level: severity level (info, warning, error, critical)
            details: additional details
        """
        results = []
        for channel in self._channels:
            handler = {
                'email': self._send_email,
                'webhook': self._send_webhook,
                'feishu': self._send_feishu,
                'dingtalk': self._send_dingtalk,
            }.get(channel['type'])

            if handler:
                try:
                    ok = handler(event, message, level, details, channel['config'])
                    results.append({'channel': channel['type'], 'sent': ok})
                except Exception as e:
                    results.append({
                        'channel': channel['type'],
                        'sent': False,
                        'error': str(e),
                    })

        return results

    def _send_email(self, event, message, level, details, config) -> bool:
        recipients = config.get('recipients', [])
        if not recipients:
            return False

        msg = MIMEMultipart()
        msg['Subject'] = f'[VeroRun Vault] [{level.upper()}] {event}'
        msg['From'] = config.get('smtp_user', '')
        msg['To'] = ', '.join(recipients)

        body = f"""
        <h2>{event}</h2>
        <p><strong>Level:</strong> {level}</p>
        <p>{message}</p>
        <pre>{json.dumps(details or {}, indent=2)}</pre>
        """
        msg.attach(MIMEText(body, 'html'))

        try:
            smtp_host = config.get('smtp_host', '')
            smtp_port = int(config.get('smtp_port', 465))
            smtp_user = config.get('smtp_user', '')
            smtp_password = config.get('smtp_password', '')

            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as smtp:
                    smtp.login(smtp_user, smtp_password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
                    smtp.starttls()
                    smtp.login(smtp_user, smtp_password)
                    smtp.send_message(msg)
            return True
        except Exception as e:
            print(f'[Vault] Email notification failed: {e}')
            return False

    def _send_webhook(self, event, message, level, details, config) -> bool:
        url = config.get('url', '')
        if not url:
            return False
        payload = {
            'event': event,
            'level': level,
            'message': message,
            'details': details,
            'timestamp': datetime.utcnow().isoformat(),
        }
        headers = config.get('headers', {})
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        return resp.status_code < 400

    def _send_feishu(self, event, message, level, details, config) -> bool:
        """Feishu bot notification."""
        url = config.get('webhook_url', '')
        if not url:
            return False

        level_colors = {'info': 'blue', 'warning': 'yellow',
                        'error': 'red', 'critical': 'purple'}
        payload = {
            'msg_type': 'interactive',
            'card': {
                'header': {
                    'title': {'tag': 'plain_text', 'content': f'Vault {event}'},
                    'template': level_colors.get(level, 'blue'),
                },
                'elements': [
                    {'tag': 'div', 'text': {'tag': 'lark_md', 'content': message}},
                    {'tag': 'hr'},
                    {'tag': 'div', 'text': {
                        'tag': 'lark_md',
                        'content': (
                            f"Level: **{level}**\n"
                            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        ),
                    }},
                ],
            },
        }
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        return result.get('code') == 0

    def _send_dingtalk(self, event, message, level, details, config) -> bool:
        """DingTalk bot notification."""
        url = config.get('webhook_url', '')
        if not url:
            return False
        payload = {
            'msgtype': 'markdown',
            'markdown': {
                'title': f'Vault - {event}',
                'text': f"## Vault {event}\n\n**Level:** {level}\n\n{message}",
            },
        }
        resp = requests.post(url, json=payload, timeout=10)
        return resp.json().get('errcode') == 0

    # ── Smart Alert: Threshold-based detection ──

    def run_smart_check(self) -> list:
        """
        Run all smart alert threshold checks. Returns list of triggered alerts.

        Checks:
          - Disk usage > 90%
          - No backup in last 48 hours
          - 3+ consecutive backup failures
          - Backup size anomaly (>50% change)
        """
        alerts = []
        alerts.extend(self._check_disk_usage())
        alerts.extend(self._check_backup_freshness())
        alerts.extend(self._check_consecutive_failures())
        alerts.extend(self._check_size_anomaly())

        for alert in alerts:
            self.send(
                event=alert['event'],
                message=alert['message'],
                level=alert['level'],
                details=alert.get('details', {}),
            )

        return alerts

    @staticmethod
    def _check_disk_usage() -> list:
        """Check if backup disk usage exceeds 90%."""
        import shutil
        import os

        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', '..', '..')
        backup_dir = os.path.join(base_dir, 'data', 'vault')
        if not os.path.isdir(backup_dir):
            return []

        total, used, free = shutil.disk_usage(backup_dir)
        usage_pct = round((total - free) / total * 100, 1) if total > 0 else 0

        if usage_pct > 90:
            return [{
                'event': 'storage.critical',
                'message': f'Backup storage critically low: {usage_pct}% used ({free / (1024**3):.1f} GB free)',
                'level': 'critical',
                'details': {'usage_pct': usage_pct, 'free_gb': round(free / (1024**3), 1)},
            }]
        elif usage_pct > 75:
            return [{
                'event': 'storage.warning',
                'message': f'Backup storage running low: {usage_pct}% used ({free / (1024**3):.1f} GB free)',
                'level': 'warning',
                'details': {'usage_pct': usage_pct, 'free_gb': round(free / (1024**3), 1)},
            }]
        return []

    @staticmethod
    def _check_backup_freshness() -> list:
        """Check if last successful backup is older than 48 hours."""
        import os
        import glob as _glob

        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', '..', '..')
        backup_dir = os.path.join(base_dir, 'data', 'vault')
        archives = sorted(
            _glob.glob(os.path.join(backup_dir, 'vault_*.tar.gz')),
            key=os.path.getmtime, reverse=True,
        )
        if not archives:
            return [{
                'event': 'backup.missing',
                'message': 'No backups found — create a backup immediately',
                'level': 'critical',
            }]

        latest_mtime = os.path.getmtime(archives[0])
        age_hours = (datetime.utcnow().timestamp() - latest_mtime) / 3600

        if age_hours > 48:
            return [{
                'event': 'backup.stale',
                'message': f'Last backup is {age_hours:.0f} hours old — over 48 hour threshold',
                'level': 'critical',
                'details': {'age_hours': round(age_hours, 1)},
            }]
        elif age_hours > 24:
            return [{
                'event': 'backup.delayed',
                'message': f'Last backup is {age_hours:.0f} hours old',
                'level': 'warning',
                'details': {'age_hours': round(age_hours, 1)},
            }]
        return []

    @staticmethod
    def _check_consecutive_failures() -> list:
        """Check for 3+ consecutive backup failures in vault_backups table."""
        try:
            from .utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT status FROM vault_backups
                ORDER BY COALESCE(completed_at, created_at) DESC
                LIMIT 10
            """)
            rows = cur.fetchall()
            cur.close()
            conn.close()

            consecutive = 0
            for row in rows:
                if row[0] == 'failed':
                    consecutive += 1
                else:
                    break

            if consecutive >= 3:
                return [{
                    'event': 'backup.consecutive_failures',
                    'message': f'{consecutive} consecutive backup failures detected — check backup engine logs',
                    'level': 'critical',
                    'details': {'consecutive_failures': consecutive},
                }]
        except Exception:
            pass
        return []

    @staticmethod
    def _check_size_anomaly() -> list:
        """Check for backup size anomaly (>50% change from average)."""
        try:
            from .utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT size_bytes FROM vault_backups
                WHERE status = 'success' AND size_bytes IS NOT NULL
                ORDER BY COALESCE(completed_at, created_at) DESC
                LIMIT 10
            """)
            rows = cur.fetchall()
            cur.close()
            conn.close()

            if len(rows) < 3:
                return []

            sizes = [r[0] for r in rows if r[0]]
            if not sizes:
                return []

            avg = sum(sizes[1:]) / len(sizes[1:])  # average excluding latest
            latest = sizes[0]
            if avg > 0 and abs(latest - avg) / avg > 0.5:
                change_pct = round((latest - avg) / avg * 100, 1)
                direction = 'increased' if change_pct > 0 else 'decreased'
                return [{
                    'event': 'backup.size_anomaly',
                    'message': f'Backup size anomaly: {direction} by {abs(change_pct)}% '
                              f'({latest / (1024**2):.1f} MB vs avg {avg / (1024**2):.1f} MB)',
                    'level': 'warning',
                    'details': {'latest_mb': round(latest / (1024**2), 1),
                                 'avg_mb': round(avg / (1024**2), 1),
                                 'change_pct': change_pct},
                }]
        except Exception:
            pass
        return []
