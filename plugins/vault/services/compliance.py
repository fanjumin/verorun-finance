#!/usr/bin/env python3
"""
Vault Compliance Reporter — Generate backup compliance reports.

Checks: backup frequency, retention compliance, restore test results, encryption status.
"""

import os
import json
from datetime import datetime, timedelta


class ComplianceReporter:
    """Generate backup compliance reports."""

    @staticmethod
    def generate_report() -> dict:
        """
        Generate a full compliance report.

        Returns:
            {
                'generated_at': str,
                'backup_frequency': dict,
                'retention_compliance': dict,
                'encryption_status': dict,
                'restore_drill': dict,
                'overall_score': int,
                'findings': list,
                'recommendations': list,
            }
        """
        report = {
            'generated_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'backup_frequency': ComplianceReporter._check_frequency(),
            'retention_compliance': ComplianceReporter._check_retention(),
            'encryption_status': ComplianceReporter._check_encryption(),
            'restore_drill': ComplianceReporter._check_restore_drill(),
        }

        findings = []
        recommendations = []
        score = 100

        # Frequency check
        freq = report['backup_frequency']
        if freq['last_backup_hours'] > 48:
            findings.append(f'No backup in last {freq["last_backup_hours"]:.0f} hours')
            recommendations.append('Configure automatic daily backup schedule')
            score -= 30
        elif freq['last_backup_hours'] > 24:
            findings.append('Last backup age exceeds 24 hours')
            recommendations.append('Consider increasing backup frequency')
            score -= 15

        # Retention check
        ret = report['retention_compliance']
        if ret['unretained_count'] > 0:
            findings.append(f'{ret["unretained_count"]} backups found but no retention policy set')
            recommendations.append('Configure retention policy (days + count)')
            score -= 10

        # Encryption check
        enc = report['encryption_status']
        if not enc['enabled']:
            findings.append('Backup encryption is disabled')
            recommendations.append('Enable AES-256-GCM encryption for all backups')
            score -= 20

        # Drill check
        drill = report['restore_drill']
        if not drill['ever_performed']:
            findings.append('Restore drill has never been performed')
            recommendations.append('Run restore drill weekly to verify backup integrity')
            score -= 15
        elif drill['last_drill_days'] > 14:
            findings.append(f'Last restore drill was {drill["last_drill_days"]} days ago')
            recommendations.append('Run restore drill more frequently')
            score -= 10

        report['findings'] = findings
        report['recommendations'] = recommendations
        report['overall_score'] = max(score, 0)
        report['compliance_level'] = (
            'Excellent' if score >= 90 else
            'Good' if score >= 70 else
            'Fair' if score >= 50 else
            'Poor'
        )

        return report

    @staticmethod
    def _check_frequency() -> dict:
        """Check backup frequency."""
        import glob as _glob
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', '..', '..')
        backup_dir = os.path.join(base_dir, 'data', 'vault')
        archives = sorted(
            _glob.glob(os.path.join(backup_dir, 'vault_*.tar.gz')),
            key=os.path.getmtime, reverse=True,
        )

        if not archives:
            return {'last_backup_hours': -1, 'total_backups': 0, 'avg_interval_hours': 0}

        now = datetime.utcnow().timestamp()
        last_hours = (now - os.path.getmtime(archives[0])) / 3600

        # Compute average interval
        intervals = []
        for i in range(len(archives) - 1):
            diff = os.path.getmtime(archives[i + 1]) - os.path.getmtime(archives[i])
            if diff < 0:
                diff = os.path.getmtime(archives[i]) - os.path.getmtime(archives[i + 1])
            intervals.append(diff / 3600)
        avg_interval = sum(intervals) / len(intervals) if intervals else 0

        return {
            'last_backup_hours': round(last_hours, 1),
            'total_backups': len(archives),
            'avg_interval_hours': round(avg_interval, 1),
        }

    @staticmethod
    def _check_retention() -> dict:
        """Check retention policy compliance."""
        from .utils import get_vault_conn
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM vault_schedules
            WHERE enabled = TRUE AND (retention_days IS NOT NULL OR retention_count IS NOT NULL)
        """)
        with_retention = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM vault_schedules WHERE enabled = TRUE")
        total = cur.fetchone()[0] or 0
        cur.close()
        conn.close()

        return {
            'policies_with_retention': with_retention,
            'total_policies': total,
            'retention_configured': with_retention > 0,
            'unretained_count': total - with_retention,
        }

    @staticmethod
    def _check_encryption() -> dict:
        """Check encryption status."""
        from plugins._base.db import get_raw_connection
        conn = get_raw_connection()
        cur = conn.cursor()
        cur.execute("SELECT config FROM plugin_registry WHERE identifier = 'vault'")
        row = cur.fetchone()
        cur.close()
        conn.close()

        enabled = False
        algorithm = 'none'
        if row and row[0]:
            cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            enc = cfg.get('encryption', {})
            enabled = enc.get('enabled', False)
            algorithm = enc.get('algorithm', 'none')

        return {
            'enabled': enabled,
            'algorithm': algorithm,
            'compliant': enabled and algorithm == 'aes256-gcm',
        }

    @staticmethod
    def _check_restore_drill() -> dict:
        """Check restore drill history."""
        from .utils import get_vault_conn
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT created_at FROM vault_audit_log
            WHERE action = 'restore.drill'
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row or not row[0]:
            return {'ever_performed': False, 'last_drill': None, 'last_drill_days': -1}

        last_drill = row[0]
        days_ago = (datetime.utcnow() - last_drill).days if last_drill else -1

        return {
            'ever_performed': True,
            'last_drill': last_drill.strftime('%Y-%m-%d %H:%M:%S'),
            'last_drill_days': days_ago,
        }
