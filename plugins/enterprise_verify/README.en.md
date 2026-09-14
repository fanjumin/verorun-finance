# Enterprise Verification (enterprise_verify)

## Overview

Enterprise Verification is VeroRun's business qualification audit plugin, providing business license OCR recognition and AI auto-audit. The plugin uses a dedicated PostgreSQL schema (`enterprise_verify`) to store certification applications, cross-reads the main database for user information, and writes back the certification status to the main `users` table after approval.

The plugin integrates SiliconFlow's DeepSeek-OCR model for intelligent business license recognition and supports both auto-approval of high-confidence results and manual review. The audit flow covers pending, approved, and rejected states.

## Features

- **OCR Business License Recognition**: Uses SiliconFlow/DeepSeek-OCR to automatically extract company name, tax ID, and other key fields from business licenses
- **AI Auto-Audit**: Configurable auto-approval of high-confidence OCR results
- **Manual Review**: Complete review UI with approve/reject actions; rejection requires a reason
- **User Info Write-back**: On approval, updates `enterprise_name`, `enterprise_tax_id`, and `enterprise_verified` on the main `users` table
- **Dedicated Database**: PostgreSQL schema `enterprise_verify` with the `enterprise_verifications` table
- **Retry Mechanism**: Configurable max OCR retry attempts

## Architecture

```
User submit (routes_user.py /api/enterprise/*)   Admin review (routes_admin.py /admin/enterprise-verifications/*)
         |                                                        |
         v                                                        v
+---------------------------------- services.py ----------------------------------+
|  +-- OCR recognition (SiliconFlow/DeepSeek-OCR)                                 |
|  +-- AI auto-audit logic                                                        |
|  +-- Submission handling                                                        |
+----------------------------------+-----------------------------------------------+
         |                                                        |
         v                                                        v
+-- Plugin schema enterprise_verify --+            +-- Main DB (read/write) --+
|  +-- enterprise_verifications       |            |  +-- users                |
+-------------------------------------+            |      (enterprise_name,   |
                                                   |       enterprise_tax_id,  |
                                                   |       enterprise_verified)|
                                                   +--------------------------+
```

**Audit state flow**:

```
pending --> approved  (approve, writes back to main users table)
pending --> rejected  (reject, reason required)
```

## Installation & Enablement

### Prerequisites

- VeroRun platform version >= 0.10.0
- SiliconFlow API Key (for DeepSeek-OCR recognition + AI audit)
- PostgreSQL database

### Install Steps

1. Place the `enterprise_verify` directory under `plugins/`
2. Make sure `enabled` is `true` in `plugin.json`
3. Restart the application — the plugin auto-creates the PostgreSQL schema `enterprise_verify`
4. Configure the plugin in Admin > "Users & Support" > "Enterprise Verification"

## Configuration

| Config Key | Type | Default | Description |
|--------|------|--------|------|
| `siliconflow_api_key` | string | "" | SiliconFlow API Key for DeepSeek-OCR and AI audit |
| `auto_approve` | boolean | false | Auto-approve high-confidence OCR results |
| `max_retry` | integer | 3 | Max OCR retry attempts |

## API Endpoints

### Admin APIs (require admin permission)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/enterprise-verifications/` | Paginated certification list (status filter, default pending) |
| POST | `/admin/enterprise-verifications/<id>/approve` | Approve a certification (writes back to main users table) |
| POST | `/admin/enterprise-verifications/<id>/reject` | Reject a certification (reason required) |
| GET | `/admin/enterprise-verifications/settings` | Get plugin config |
| POST | `/admin/enterprise-verifications/settings` | Save plugin config |

### User APIs

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/enterprise/submit` | Submit a certification application (upload business license, etc.) |

### Provided Hooks

| Hook Identifier | Description |
|-------------|------|
| `enterprise_verify/submit` | Submit an enterprise certification |
| `enterprise_verify/audit` | Run an audit |
| `enterprise_verify/ocr_recognize` | Trigger OCR recognition |

## License

This plugin is part of the VeroRun platform and follows the platform's unified license agreement.
