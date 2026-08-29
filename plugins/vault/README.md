# Vault (vault)

> Data Vault — full/incremental backups, AES-256-GCM encryption, scheduled backups, audit logging, multi-target storage, and one-click restore.

Version: **2.1.1**

## Overview

Vault is VeroRun's data backup and restore plugin, delivering enterprise-grade data protection: full database backups via `pg_dump`, tar.gz archiving, gzip compression, AES-256-GCM encryption, HMAC-SHA256 signature verification, multi-target storage (local / S3 / OSS / Azure / GCS / SFTP / WebDAV), cron-based scheduling, audit logging, compliance reporting, restore drills with sandbox verification, and point-in-time recovery (PITR).

## Features

- **Full backup**: one-click full database snapshot (archive format `vault_*.tar.gz`)
- **Incremental / differential**: `backup_type` (full / incremental / differential) reserved and orchestratable
- **Compression**: gzip archive compression to reduce storage and transfer cost
- **AES-256-GCM encryption**: optional stream encryption for data at rest
- **HMAC-SHA256 signing**: sign backups with `VAULT_SIGNING_KEY` and verify integrity against tampering
- **Scheduled backups**: cron-expression driven (`vault_schedules`), with enable/disable; integrates with the orchestrator `cron_jobs` (a daily backup at 03:00 UTC is seeded by default)
- **Multi-target storage**: local + six remote backends (S3 / OSS / Azure / GCS / SFTP / WebDAV), with default target and connection testing
- **3-2-1 rotation upload**: push the latest backup to remote targets following the 3-2-1 rule
- **Storage tier report**: per-target distribution and usage overview
- **One-click restore**: restore the database from an archive, with content preview, scoped restore, and target database/host options
- **Point-in-time recovery (PITR)**: restore to a specific timestamp
- **Restore drill**: sandbox restore → verification → cleanup to prove backups are recoverable
- **Audit log**: full record of backup/restore/schedule/storage operations for compliance
- **Compliance report**: automatic checks for retention policy, restore drills, and encryption status
- **Health check**: backup freshness, storage usage, and a health score
- **Trend analysis**: size trend for the latest 90 successful backups
- **Notifications**: Email / Webhook / Feishu / DingTalk push on success/failure
- **Admin UI**: complete embedded management interface (dashboard / backups / restore / schedules / storage / settings / audit)

## Architecture

### Data isolation

Following [plugin-standard v1.3 §9.1](docs/plugin-standard-v1.3.md), Vault uses a **dedicated database schema `vault`** and never writes to the shared `public` schema:

```sql
CREATE SCHEMA IF NOT EXISTS vault;
SET search_path TO vault, public;
```

- All business access goes through `get_vault_conn()` with a fixed `search_path = vault, public`: plugin tables resolve to `vault`, while system tables fall back through `public` without interference.
- Backup archives are still stored under `data/vault/` (controlled by `backup_dir`).

### Backup pipeline

```
backup_engine.py  (orchestrator: create_full_backup)
        │
        ├─ dumper.py        pg_dump export → tar.gz archive
        ├─ compressor.py    gzip compression
        ├─ encryptor.py     AES-256-GCM stream encryption (optional)
        ├─ uploader.py      multi-target upload (StorageRouter)
        └─ notifier.py      success/failure notifications (Email/Webhook/Feishu/DingTalk)
```

### Restore pipeline

```
restore_engine.py  (orchestrator: restore / restore_pitr / drill_restore)
        │
        ├─ preview         preview archive contents
        ├─ restore         decompress → decrypt → psql restore (optional scope/target db)
        ├─ pitr            point-in-time recovery
        └─ drill_restore   sandbox restore → verify → cleanup
```

### Module map

| Module | Responsibility |
|--------|----------------|
| `backup_engine.py` | Backup pipeline orchestrator |
| `dumper.py` | Database export (pg_dump) and archiving |
| `compressor.py` | gzip compression |
| `encryptor.py` | AES-256-GCM encryption |
| `validator.py` | HMAC-SHA256 signing / integrity verification |
| `uploader.py` | Multi-target upload |
| `restore_engine.py` | Restore / PITR / drill orchestration |
| `scheduler.py` | Cron scheduling (`vault_schedules`) |
| `compliance.py` | Compliance report (retention / drill / encryption) |
| `audit.py` | Audit log recording and querying |
| `notifier.py` | Notification dispatch (Email / Webhook / Feishu / DingTalk) |
| `storage/base.py` | Storage router & abstract base (local/s3/oss/azure/gcs/sftp/webdav) |
| `utils.py` | Connection helper (`get_vault_conn`), idempotent schema setup (`ensure_schema`), config loader |

## Database schema

All tables live in the `vault` schema, created idempotently by `migrations/001_initial.sql` (`IF NOT EXISTS`):

| Table | Purpose |
|-------|---------|
| `vault_backups` | Backup job records (type/status/size/checksum/content summary/timestamps) |
| `vault_schedules` | Backup schedules (cron/retention/window/pre-post hooks/enabled) |
| `vault_audit_log` | Audit log (action/resource/operator/IP/details) |
| `vault_storage_targets` | Storage target configs (type/config/default flag/connection test) |

## Directory layout

```
vault/
├── __init__.py              # Plugin entry (on_install / on_enable / route registration / schedule seeding)
├── routes.py                # Admin pages and API routes
├── run_scheduler.py         # Standalone scheduler entry point
├── plugin.json              # Plugin metadata and default config
├── README.md / README_CN.md # English / Chinese documentation
├── migrations/
│   └── 001_initial.sql      # Idempotent migration: vault schema + 4 tables + indexes
├── services/
│   ├── __init__.py
│   ├── backup_engine.py     # Backup orchestrator
│   ├── dumper.py            # Database export
│   ├── compressor.py        # Compression
│   ├── encryptor.py         # AES-256-GCM encryption
│   ├── validator.py         # HMAC signing / verification
│   ├── uploader.py          # Upload
│   ├── restore_engine.py    # Restore / PITR / Drill
│   ├── scheduler.py         # Scheduling
│   ├── compliance.py        # Compliance report
│   ├── audit.py             # Audit log
│   ├── notifier.py          # Notifications
│   ├── utils.py             # Connection helper / idempotent schema setup
│   └── storage/
│       ├── base.py          # Storage router abstract base
│       ├── local.py / s3.py / oss.py / azure.py / gcs.py / sftp.py / webdav.py
├── static/
│   ├── vault.css            # Admin styles
│   └── vault.js             # Admin scripts
├── templates/               # Page templates (vault / audit / restore / schedules / settings / storage)
└── i18n/
    ├── en.yml               # English localization
    └── zh-CN.yml            # Simplified Chinese localization
```

## Installation & enablement

1. The plugin ships with VeroRun's default plugin set; no separate install step is required.
2. Install dependencies:

```bash
pip install croniter cryptography paramiko requests
# optional: pip install boto3 oss2 azure-storage-blob google-cloud-storage
```

3. Enable the plugin under Admin → Plugin Manager. On enable, Vault automatically:
   - creates the backup directory `data/vault/`;
   - runs the idempotent migration (creates the `vault` schema and tables, owned by the application database user);
   - seeds a daily backup job in the orchestrator (default `0 3 * * *` UTC).
4. Open **System → Vault** in the admin UI to manage backups.

> `ensure_schema()` is also triggered before every Vault request as an idempotent safety net, keeping the schema ready at all times (fresh environments create tables as the app user, so ownership is always correct).

## Configuration

Defaults live in the `config` field of `plugin.json` and can be overridden at runtime via the Admin Settings page:

```json
{
  "backup_dir": "data/vault",
  "keep_days": 30,
  "include_files": true,
  "include_config": true,
  "encryption": { "enabled": false, "algorithm": "aes256-gcm", "key_source": "env" },
  "compression": { "algorithm": "gzip", "level": 6 },
  "storage": { "type": "local", "s3_bucket": "", "s3_region": "", "s3_access_key": "", "s3_secret_key": "", "oss_endpoint": "", "oss_bucket": "", "oss_access_key": "", "oss_secret_key": "" },
  "schedule": { "enabled": false, "interval_hours": 24 },
  "notifications": {
    "email": { "enabled": false, "smtp_host": "", "smtp_port": 465, "smtp_user": "", "smtp_password": "", "recipients": [] },
    "webhook": { "enabled": false, "url": "", "headers": {} },
    "feishu": { "enabled": false, "webhook_url": "" },
    "dingtalk": { "enabled": false, "webhook_url": "" }
  }
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `backup_dir` | Backup archive directory (relative to project root) | `data/vault` |
| `keep_days` | Retention days for auto cleanup (`/api/cleanup`) | `30` |
| `include_files` | Include files in backup | `true` |
| `include_config` | Include configuration in backup | `true` |
| `encryption.enabled` | Enable AES-256-GCM encryption | `false` |
| `encryption.algorithm` | Encryption algorithm | `aes256-gcm` |
| `encryption.key_source` | Encryption key source | `env` |
| `compression.algorithm` | Compression algorithm | `gzip` |
| `compression.level` | Compression level | `6` |
| `storage.type` | Default storage type | `local` |
| `schedule.enabled` | Enable the default scheduled backup | `false` |
| `schedule.interval_hours` | Schedule interval (hours) | `24` |
| `notifications.*` | Per-channel notification settings | all disabled |

## API endpoints

> All endpoints require an admin JWT (`sso_token` cookie or `?token=`). Unauthenticated non-AJAX requests redirect to `/admin/login`.

### Page routes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/vault/` | Dashboard |
| `GET` | `/admin/vault/backups` | Backup list |
| `GET` | `/admin/vault/restore` | Restore wizard |
| `GET` | `/admin/vault/schedules` | Schedule management |
| `GET` | `/admin/vault/storage` | Storage configuration |
| `GET` | `/admin/vault/settings` | Plugin settings |
| `GET` | `/admin/vault/audit` | Audit log |

### Backup API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/vault/api/backup/create` | Trigger a backup (full; incremental/differential compatible) |
| `POST` | `/admin/vault/api/create` | Trigger a backup (legacy, kept for compatibility) |
| `GET` | `/admin/vault/api/backup/list` | List backups (search/filter/pagination) |
| `GET` | `/admin/vault/api/list` | List backups (legacy) |
| `GET` | `/admin/vault/api/backup/detail/<label>` | Backup detail + content preview |
| `GET` | `/admin/vault/api/backup/download/<label>` | Download a backup |
| `DELETE` | `/admin/vault/api/backup/delete/<label>` | Delete a backup (requires `confirm`) |
| `DELETE` | `/admin/vault/api/delete/<label>` | Delete a backup (legacy) |
| `DELETE` | `/admin/vault/api/cleanup` | Clean up backups older than `keep_days` |
| `POST` | `/admin/vault/api/backup/sign/<label>` | HMAC-SHA256 sign (requires `VAULT_SIGNING_KEY`) |
| `GET` | `/admin/vault/api/backup/verify/<label>` | Signature & integrity verification |

### Restore API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/vault/api/restore/preview` | Preview backup contents |
| `POST` | `/admin/vault/api/restore` | Execute restore (optional scope / target_db / target_host) |
| `POST` | `/admin/vault/api/restore/pitr` | Point-in-time recovery (`target_time`, ISO format) |
| `POST` | `/admin/vault/api/restore/drill` | Restore drill (sandbox restore → verify → cleanup) |

### Schedule API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/vault/api/schedule/list` | List schedules |
| `POST` | `/admin/vault/api/schedule/create` | Create a schedule (name + cron_expression) |
| `PUT` | `/admin/vault/api/schedule/<id>` | Update a schedule |
| `DELETE` | `/admin/vault/api/schedule/<id>` | Delete a schedule |
| `POST` | `/admin/vault/api/schedule/<id>/toggle` | Enable / disable a schedule |

### Storage API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/vault/api/storage/list` | List storage targets |
| `POST` | `/admin/vault/api/storage/create` | Create a storage target |
| `PUT` | `/admin/vault/api/storage/<id>` | Update a storage target |
| `DELETE` | `/admin/vault/api/storage/<id>` | Delete a storage target |
| `POST` | `/admin/vault/api/storage/<id>/test` | Test connection |
| `POST` | `/admin/vault/api/storage/rotate` | 3-2-1 rotation upload of the latest backup |
| `GET` | `/admin/vault/api/storage/tier/report` | Storage tier report |

### Other APIs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/vault/api/health` | Health check (score / last backup / next schedule / disk usage) |
| `GET` | `/admin/vault/api/audit` | Audit log query (action/resource_type/operator/limit/offset) |
| `GET` | `/admin/vault/api/compliance/report` | Compliance report |
| `GET` | `/admin/vault/api/trend` | Backup size trend (latest 90 successful backups) |

### Hooks provided

| Hook identifier | Description |
|-----------------|-------------|
| `vault/create_backup` | Trigger a backup |
| `vault/list_backups` | List backups |
| `vault/delete_backup` | Delete a backup |
| `vault/health_check` | Backup system health check |
| `vault/audit_log` | Audit log query |

## Security

- **Authentication**: every Vault API is gated by admin JWT validation (`validate_token` + `is_admin`).
- **Encryption**: optional AES-256-GCM stream encryption for backups (`encrypt_stream`).
- **Tamper-proofing**: HMAC-SHA256 signing/verification enabled via the `VAULT_SIGNING_KEY` environment variable.
- **Audit trail**: all key operations (backup/restore/schedule/storage) are written to `vault_audit_log`.
- **Delete confirmation**: the delete endpoint requires a `confirm` flag to prevent accidental deletion.
- **Connection isolation**: plugin data is confined to the `vault` schema and never written to the shared public area.

## Notifications

Email (SMTP), Webhook, Feishu, and DingTalk channels are supported, with automatic push on backup success/failure. Channel settings live under the `notifications` section of the plugin config.

## Dependencies

| Dependency | Purpose | Required |
|------------|---------|----------|
| `croniter` | cron expression parsing | ✅ |
| `cryptography` | AES-256-GCM encryption | ✅ |
| `paramiko` | SFTP storage backend | ✅ |
| `requests` | HTTP notifications | ✅ |
| `boto3` | S3 storage backend | ⭕ optional |
| `oss2` | Alibaba Cloud OSS backend | ⭕ optional |
| `azure-storage-blob` | Azure Blob backend | ⭕ optional |
| `google-cloud-storage` | GCS backend | ⭕ optional |

## Troubleshooting

- **`storage/list` returns 500 / relation does not exist**: make sure 2.1.1+ is deployed and the service restarted; `ensure_schema()` creates the tables in the `vault` schema automatically. Legacy `public.vault_*` tables left over from older versions are ignored and may be cleaned up through the ops process after confirmation.
- **Encryption raises `ValueError`**: encryption is skipped when `encryption.enabled` is false or no key is configured — expected behavior.
- **Signing API returns 400**: set the `VAULT_SIGNING_KEY` environment variable first.
- **Schedules do not fire**: confirm the schedule has `enabled = true` and a valid cron expression; the orchestrator entry point is `run_scheduler.py`.

## License

This plugin is part of the VeroRun project and is governed by the VeroRun project license.
