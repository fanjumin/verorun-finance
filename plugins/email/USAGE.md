# Email Service — Usage Guide

## Quick Start

1. **Configure the mail server** — Admin > Users & Support > Email Management > Settings, or use environment variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `IMAP_HOST`, `IMAP_PORT`).
2. **Send mail** — compose a new email (text / HTML body; attachments up to 10MB each, 50MB total).
3. **Inbox** — incoming mail is fetched via IMAP and auto-marked as read.
4. **Contacts** — automatically merged from sent recipients and contact-form submissions.

## Config Priority

Env vars > PluginManager config > system_config > defaults (Aliyun Enterprise Mail).

## Admin API

- `GET /admin/email/inbox` — inbox list
- `POST /admin/email/send` — send an email
- `GET /admin/email/sent` — sent list
- `GET /admin/email/settings`、`POST /admin/email/settings` — read / save settings
- `GET /admin/email/attachment/<uid>/<filename>` — download an attachment

## Tips

- Sensitive fields such as passwords are masked in the UI.
- Multi-encoding auto-detection (UTF-8 / GBK / GB2312 / Latin-1).
- Provides `email/send`, `email/send_contact` and `email/get_config` hooks for other modules.
