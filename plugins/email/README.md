# Email Service (email)

## Overview

Email Service is VeroRun's unified email plugin, providing full SMTP send and IMAP receive capabilities with inbox management, compose, attachments, and contact management. The plugin uses a dedicated PostgreSQL schema `email`, isolated from the main database.

The plugin supports SMTP/IMAP protocols. Since v1.6.0, 22 popular email providers are preconfigured (`mail_providers.py`) — selecting a provider or entering an email address and clicking "Auto-detect" fills in the SMTP/IMAP host and port (only empty fields are populated; manual config is never overwritten). Custom providers are still supported. Config priority: environment variables > PluginManager > system_config > defaults (all empty) > domain auto-derivation.

Since v1.7.0, email capabilities were significantly enhanced: CC/BCC, drafts, full-text inbox search, bulk mailbox operations (delete / mark read-unread / move), and forward. The three agent hooks `email/send`, `email/send_contact`, `email/get_config` are wired as system-level MCP tools (`email_send` / `email_send_contact` / `email_get_config`), enabling both agents and human users to send and compose email.

Since v1.8.0, enterprise-grade security controls were added: attachment extension blocking, daily send quota, recipient domain allowlist, and a private-network SMTP/IMAP probe switch. The three hooks are also fulfilled as inter-plugin synchronous action surfaces in addition to the MCP tool surface.

## Features

- **SMTP send**: plain text and HTML emails, SSL/TLS encryption, CC/BCC support
- **IMAP receive**: inbox list, email detail reading, auto-mark-as-read
- **Inbox search**: server-side IMAP SEARCH (FROM/SUBJECT/TEXT union), with client-side in-memory fallback
- **Drafts**: save drafts mid-composition, draft list/edit/delete, auto-cleanup after send
- **Bulk operations**: multi-select delete (with confirmation), mark read/unread, move to IMAP folders
- **Forward**: one-click forward with `Fwd:` subject prefix and quoted original
- **Attachments**: upload and download; 10MB per attachment, 50MB total
- **Contact management**: Python-level merge of sent-mail contacts and contact-form contacts
- **Multi-encoding**: UTF-8, GBK, GB2312, Latin-1 auto-detection
- **MIME support**: full multipart/alternative and multipart/mixed structure
- **Send log**: all sent emails recorded in `email_sent` table
- **Contact form integration**: `send_contact_email` supports branded contact-form emails
- **Agent tools**: `email_send` / `email_send_contact` / `email_get_config` as system-level MCP tools
- **Independent database**: PostgreSQL schema `email` with `email_sent` and `email_drafts` tables
- **Flexible config**: environment variables, PluginManager, and system_config three-tier sources
- **Quick config**: 22 preconfigured providers (Aliyun / NetEase / QQ / Tencent Enterprise / Sina / Sohu / 139 / 263 / Foxmail / Gmail / Outlook / Yahoo / iCloud / AOL / Zoho / Yandex / Fastmail / Proton, etc.)
- **Security controls**: blocked attachment extensions, daily send quota, recipient domain allowlist, private-network probe switch

## Architecture

```
+--------------------------------------------------------------+
|                        Admin UI                              |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      Routes (routes.py)                       |
|  /admin/email/*                                              |
|  +-- /inbox          inbox list (IMAP, ?q= search)            |
|  +-- /read/<uid>     email detail                            |
|  +-- /send           send email (cc/bcc)                      |
|  +-- /drafts         drafts (GET list / POST save)            |
|  +-- /drafts/<id>    delete draft (DELETE)                    |
|  +-- /folders        IMAP folder list (for bulk move)         |
|  +-- /batch          bulk ops (delete/read/unread/move)       |
|  +-- /sent           sent list                                |
|  +-- /contacts       contacts (multi-source merge)             |
|  +-- /settings       config read/write (provider auto-detect)  |
|  +-- /attachment/<uid>/<filename>  attachment download         |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      Services (services.py)                    |
|  IMAP connect / inbox fetch / read / attachment / send /     |
|  drafts / folders / bulk ops / sent / contact-form /          |
|  config merge / MIME decode / body extraction                 |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      Data layer (models.py)                   |
|  PG Schema: email                                             |
|  +-- email_sent     sent email records                        |
|  +-- email_drafts   drafts (attachments as JSON refs)          |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                  System-level MCP (email_mcp_server.py)       |
|  Registered via agent_matrix register_system_mcp_server:       |
|  +-- email_send          send email (cc/bcc/attachments)      |
|  +-- email_send_contact   contact-form email                   |
|  +-- email_get_config    masked config query                  |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      External services                        |
|  +-- SMTP Server (SSL/TLS)                                    |
|  +-- IMAP Server                                              |
+--------------------------------------------------------------+
```

**Config priority:**

```
Environment variables (SMTP_HOST, SMTP_PORT, ...)
    |
    v
PluginManager config (email plugin config)
    |
    v
Main DB system_config table (legacy compatibility)
    |
    v
Defaults (all empty)
    |
    v
Domain auto-derivation (mail_providers.py, empty fields only)
```

## Directory Layout

```
email/
├── plugin.json                  # Plugin metadata
├── __init__.py                  # Entry: blueprint registration, hooks
├── mail_providers.py            # Preconfigured provider catalog
├── email_mcp_server.py          # System-level MCP server
├── models.py                    # Data models (PG schema: email)
├── routes.py                    # Admin API routes
├── services.py                  # Core email logic (SMTP/IMAP, MIME, attachments, drafts, search)
├── crypto.py                    # Attachment content-type helpers
├── i18n/
│   ├── en.yml                   # English i18n
│   └── zh-CN.yml                # Chinese i18n
└── templates/
    └── admin_email.html          # Admin page template
```

## Installation & Enablement

### Prerequisites

- VeroRun platform >= 0.10.0
- Working SMTP and IMAP servers
- PostgreSQL database

### Steps

1. Place the `email` directory under `plugins/`.
2. Configure mail server parameters (via environment variables or the admin settings page).
3. Restart the app; the plugin auto-creates PostgreSQL schema `email` and the `email_sent` table.
4. Open **Users & Support → Email Management** in the admin UI.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SMTP_HOST` | SMTP server hostname | (empty, user-configured) |
| `SMTP_PORT` | SMTP port | (empty, user-configured) |
| `SMTP_USER` | SMTP login username | - |
| `SMTP_PASS` | SMTP login password | - |
| `SMTP_FROM` | From address | same as SMTP_USER |
| `IMAP_HOST` | IMAP server hostname | (empty, user-configured) |
| `IMAP_PORT` | IMAP port | (empty, user-configured) |
| `CONTACT_TO` | Contact-form recipient | - |
| `ALERT_RECIPIENT` | Alert / morning-brief recipient | - |

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `smtp_host` | string | "" | SMTP server hostname |
| `smtp_port` | integer | 0 | SMTP port (465=SSL, 587=STARTTLS) |
| `smtp_user` | string | "" | SMTP username |
| `smtp_pass` | string | "" | SMTP password (masked in UI) |
| `smtp_from` | string | "" | From address |
| `imap_host` | string | "" | IMAP server hostname |
| `imap_port` | integer | 0 | IMAP port |
| `alert_recipient` | string | "" | Alert/brief recipient (empty = skip email channel; env `ALERT_RECIPIENT` overrides) |
| `blocked_attachment_exts` | string | `.exe,.bat,.cmd,.scr,.js,.vbs,.ps1,.jar,.msi` | Blocked attachment extensions, comma-separated (empty = allow all) |
| `allow_private_targets` | boolean | false | Allow test-config to probe loopback/private SMTP/IMAP targets (default: blocked) |
| `daily_send_quota` | integer | 200 | Max emails per day (0 = unlimited); counted from `email_sent` records |
| `recipient_domain_allowlist` | string | "" | Allowed recipient domains, comma-separated (empty = allow all) |

## API Endpoints

### Admin API (admin required)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/email/inbox` | Inbox list (50 per page; `?q=` keyword search) |
| GET | `/admin/email/read/<uid>` | Email detail (body + attachments) |
| POST | `/admin/email/send` | Send email (text, HTML, attachments, reply, cc/bcc) |
| GET | `/admin/email/drafts` | Draft list (most recent first) |
| POST | `/admin/email/drafts` | Save/update draft (body with `draft_id` = update) |
| DELETE | `/admin/email/drafts/<id>` | Delete draft |
| GET | `/admin/email/folders` | IMAP folder list (for bulk move) |
| POST | `/admin/email/batch` | Bulk op: `action=delete\|read\|unread\|move` + `uids` (move requires `folder`) |
| GET | `/admin/email/sent` | Sent email list |
| GET | `/admin/email/contacts` | Contact list (merged sent + contact-form) |
| GET | `/admin/email/settings` | Config read (sensitive fields masked) |
| POST | `/admin/email/settings` | Config save (with domain auto-fill) |
| GET | `/admin/email/providers` | Preconfigured provider catalog (22) |
| POST | `/admin/email/test-config` | Test SMTP/IMAP connectivity and auth |
| GET | `/admin/email/attachment/<uid>/<filename>` | Download attachment |

### Send email request body

```json
{
  "to": "recipient@example.com",
  "subject": "Subject",
  "body": "Plain text body",
  "body_html": "<h1>HTML body</h1>",
  "cc": "cc@example.com",
  "bcc": "bcc@example.com",
  "attachments": [
    {
      "filename": "report.pdf",
      "data": "<base64 data>",
      "content_type": "application/pdf"
    }
  ],
  "reply_to_uid": 123
}
```

### System-level MCP tools (Agent invocation)

| Tool | Description | Key params |
|------|-------------|------------|
| `email_send` | Send email (cc/bcc/attachments) | `to`, `subject`, `body`, `body_html?`, `cc?`, `bcc?`, `attachments?` |
| `email_send_contact` | Contact-form email (to `CONTACT_TO`) | `name`, `email`, `subject`, `message` |
| `email_get_config` | Current config summary (masked password) | none |

> MCP tool namespace is `mcp__email__*`; config is snapshotted by the main process into `EMAIL_MCP_*` environment variables (including domain derivation results), so child processes have no DB dependency.

## Dependencies

### Internal

| Dependency | Purpose |
|-----------|---------|
| `plugins._base.db` | Plugin base DB connection |
| `auth-center.models` | Main DB read (system_config, contact_messages) |
| `auth-center.services.brand_service` | Brand settings for contact-form emails |

### External

| Dependency | Purpose |
|-----------|---------|
| Python `smtplib` | SMTP protocol |
| Python `imaplib` | IMAP protocol |
| Python `email` | MIME building and parsing |

### Hooks provided

| Hook | Description |
|------|-------------|
| `email/send` | Send email |
| `email/send_contact` | Send contact-form email |
| `email/get_config` | Get email config |

## Event-Driven Research Delivery

Beyond passive email send/receive, the email plugin serves as the **overall delivery exit for VeroRun research events** — it consumes events from `stock_analysis` via the platform Hook Registry (`get_event_handlers` subscribe, `do_action` emit — two isolated channels from the `get_event_bus` event bus) and renders them into emails. This is the consumer side of the morning-brief push loop and alert email loop.

### Subscribed events

| Event | Handler | Description |
|-------|---------|-------------|
| `stock.alert.triggered` | `push_alert` | Market/signal alert email. Respects the `channels` config — sends only when the alert declares `email` in its channels and a recipient is configured; safely skips (no send, no error) if no recipient is set. |
| `stock.morning_brief.ready` | `push_morning_brief` | Scheduled morning brief email. Subscribes to the trading-day 08:30 (Mon–Fri) brief payload from `stock_analysis` scheduler, rendered to plain/HTML email via `_format_morning_mail`. |

> Emit side lives in `stock_analysis`: `alert_engine.py` emits via `do_action("stock.alert.triggered")`; `morning_brief.py` `dispatch_morning_brief()` emits via `do_action("stock.morning_brief.ready", payload=...)`. Both sides go through the Hook Registry.

## Menu

- **Users & Support** — Email Management

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.
