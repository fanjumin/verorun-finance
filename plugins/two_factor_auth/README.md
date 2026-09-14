# Two-Factor Authentication (two_factor_auth)

> TOTP-based two-factor authentication for VeroRun — compatible with Google Authenticator / Microsoft Authenticator, with one-time recovery codes, device-bound short-lived tokens, and a full operation audit trail.

Version: **1.2.0**

## Overview

The Two-Factor Authentication plugin adds a second factor to VeroRun logins. It issues a TOTP secret bound to the account (AES-256-GCM encrypted, bound to `user_id`), renders an Authenticator-style setup wizard (auto-shown QR code, scan / manual-key entry, 30s code countdown, recovery codes), intercepts logins via the `auth.before_issue_session` hook when the user has 2FA enabled, and completes login through a device-bound challenge page. It ships as a **pure plugin**: no core files are modified, and disabling the plugin restores system behavior exactly as if it were never installed.

## Features

- **Authenticator-style setup flow** — the setup page opens directly to a QR code (no extra button), keeps the QR visible while entering the code, offers a *scan / enter setup key* toggle with copy, a 1→2→3 step wizard, a 30-second code countdown, and copy-all / print for recovery codes
- **Standard TOTP (RFC 6238 / Key Uri Format)** — 6-digit codes, 30-second period, SHA-1, BASE32 secrets accepted by Google Authenticator, Microsoft Authenticator, 1Password, 2FAS, Aegis, etc.
- **Login challenge** — when a user with 2FA enabled signs in, login is blocked once and the user is routed to the challenge page for a second factor before the SSO session is issued
- **Recovery codes** — 10 single-use 10-character codes (bcrypt-hashed at rest), consumed on use
- **Brute-force protection** — failed attempts are counted atomically and the account is locked for `lockout_seconds` after `max_failed_attempts` failures (no TOCTOU race)
- **Short-lived, device-bound tokens** — the admin JWT never enters a URL; a 3-minute `setup_token` (bound to IP prefix + user-agent) is exchanged in memory, and login challenges are bound to the generating device and consumed atomically against replay
- **Encryption at rest** — TOTP secrets encrypted with AES-256-GCM, keyed via the stable `TOTP_MASTER_KEY` environment variable, with `user_id` as AAD
- **Operation audit** — `setup_token_issued`, `setup_init`, `setup_enable`, `setup_disable`, `challenge_generated`, `login_success/failed` and binding-mismatch events are written to `audit_log`
- **Auto cleanup** — expired challenges and setup tokens are purged hourly by an APScheduler job started on enable
- **i18n** — English and Simplified Chinese UI, following the project i18n standard (English keys, English default)

## Architecture

### Data isolation

Following the plugin standard, all plugin data lives in its own schema `two_factor_auth` and is never written to the shared `public` area:

```sql
CREATE SCHEMA IF NOT EXISTS two_factor_auth;
SET search_path TO two_factor_auth, public;
```

All plugin access uses `get_two_factor_db()` (pooled connection with `search_path` + UTC time semantics). Migrations are serialized with a `pg_try_advisory_xact_lock` and a run-once `schema_migrations` table, so concurrent gunicorn workers cannot dead-lock on DDL.

### Login interception

```
Login → core issue_auth_session
         │
         ▼
   apply_filters('auth.before_issue_session', …)   ← two_factor_auth filter
         │
         ├─ scenario != 'login'            → pass (None)
         ├─ user has no 2FA enabled        → pass (None)
         ├─ 2FA enabled  → return {blocked, challenge_token, redirect}
         │                 → core raises TwoFactorRequired → needs_2fa response
         │                 → browser opens /plugin/two_factor_auth/challenge-page
         └─ any exception                  → fail-open (login proceeds), logged
```

The pre-check is **fail-open**: on any exception (including DB failure) the user is signed in without 2FA and a warning is logged — it can never lock everyone out.

### Module map

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | Plugin lifecycle (`on_install`/`on_enable`/`on_disable`/`on_uninstall`), filter registration, cleanup scheduler |
| `routes.py` | Flask Blueprint: setup portal, setup page, init/verify/disable, status, challenge verify, session issuing |
| `services.py` | TOTPService (generate/encrypt/decrypt/verify, provisioning URI, QR code), recovery codes, `pre_login_check` |
| `models.py` | DB access layer, schema switching, advisory-lock migrations |
| `plugin.json` | Manifest: metadata, config defaults, menu, dependencies |

## Database schema

All tables live in the `two_factor_auth` schema (migrations `0001_init.sql`, `0002_pending_timestamptz.sql`, `0003_security_hardening.sql`):

| Table | Purpose |
|-------|---------|
| `setup_tokens` | Short-lived portal tokens (3 min, IP/UA bound, pending TOTP secret for idempotent init) |
| `user_totp` | Per-user encrypted TOTP secret, enable state, recovery code hashes, failure/lock counters |
| `two_factor_challenges` | Login challenges (5 min, device bound, atomically consumed) |
| `audit_log` | Operation audit events |
| `schema_migrations` | Applied migration files (run-once) |

## Directory layout

```
plugins/two_factor_auth/
├── __init__.py              # Plugin lifecycle + hook registration + cleanup scheduler
├── routes.py                # Blueprint (setup portal / setup page / init / verify / disable / status / challenge)
├── services.py              # TOTP / AES-GCM / recovery codes / pre_login_check
├── models.py                # DB access + advisory-lock migrations
├── plugin.json              # Manifest and default config
├── CHANGELOG.md
├── README.md / README_CN.md # English / Chinese documentation (this file / Chinese manual)
├── migrations/
│   ├── 0001_init.sql
│   ├── 0002_pending_timestamptz.sql
│   └── 0003_security_hardening.sql
├── templates/
│   ├── setup.html           # Authenticator-style setup wizard
│   └── challenge.html       # Login second-factor page
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## Installation & enablement

1. The plugin ships with VeroRun's plugin set; no separate download is required.
2. Install dependencies:

```bash
pip install pyotp qrcode bcrypt cryptography
```

3. **Configure the encryption key first** (required; see *Configuration*). Set a stable `TOTP_MASTER_KEY` in the `.env` of every service that hosts the plugin, then restart.

4. Enable the plugin under **Admin → Plugin Manager**. On enable, the plugin automatically:
   - runs the idempotent schema migrations;
   - registers the `auth.before_issue_session` filter;
   - starts the hourly cleanup scheduler.

5. Open the **Security & Compliance → 两步验证设置 (2FA Settings)** menu in the admin panel to bind an account.

> Disabling the plugin unregisters the filter and stops the scheduler; uninstalling drops the `two_factor_auth` schema.

## Configuration

Defaults come from the `config` field of `plugin.json` and can be overridden per-environment through `TOTP_*` environment variables (environment wins):

| Key | Env override | Description | Default |
|-----|--------------|-------------|---------|
| `max_failed_attempts` | `TOTP_MAX_FAILED_ATTEMPTS` | Failed attempts before lockout | `5` |
| `lockout_seconds` | `TOTP_LOCKOUT_SECONDS` | Lockout duration (seconds) | `900` |
| `recovery_code_count` | `TOTP_RECOVERY_CODE_COUNT` | Recovery codes generated on enable | `10` |
| `issuer_name` | `TOTP_ISSUER_NAME` | Issuer shown in Authenticator apps | `VeroRun` |
| `totp_valid_window` | `TOTP_VALID_WINDOW` | TOTP tolerance window (±30 s per step) | `1` |
| — | `TOTP_MASTER_KEY` | **Encryption key (≥32 chars)** — see warning below | *(none)* |

### `TOTP_MASTER_KEY` — read this

```env
TOTP_MASTER_KEY=<at least 32 random characters>
```

- It is hashed (SHA-256) into the 32-byte AES-256-GCM key that encrypts every TOTP secret.
- **It must be stable.** Changing it after users have enrolled makes their secrets undecryptable and locks them out of 2FA logins.
- **It must be configured before enabling users.** If it is missing or too short, `/setup/init` and `/challenge/verify` raise a `RuntimeError` (HTTP 500).
- Generate one: `openssl rand -hex 32`, keep a backup in a safe place, and reuse the same value across every environment that hosts this plugin.

## API endpoints

> All endpoints live under the auto-registered prefix `/plugin/two_factor_auth`. Setup endpoints accept the short-lived `setup_token` via `?setup_token=` or the `Authorization: Bearer` header; admin-only state endpoints require a login JWT.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `POST` | `/setup-token` | Exchange a login JWT (`Bearer`) for a 3-minute `setup_token` (issued in memory — the JWT never enters a URL) |
| `GET` | `/setup-page` | Setup wizard (`?setup_token=…` or `sso_token` cookie) |
| `POST` | `/setup/init` | Generate TOTP secret + QR code; **idempotent** — repeat calls return the stored pending secret (page reload keeps the same QR) |
| `POST` | `/setup/verify` | Validate the 6-digit code (and, for re-enrollment, the current code of the existing 2FA), enable 2FA, return recovery codes |
| `POST` | `/setup/disable` | Disable 2FA (requires a valid current code) |
| `GET` | `/status` | Enrollment state (requires login JWT) |
| `GET` | `/challenge-page` | Login second-factor page (`?challenge_token=…`) |
| `POST` | `/challenge/verify` | Verify the second factor, atomically consume the challenge, issue the final SSO session |

## Security

- **No long-lived JWT in URLs** — the admin front-end exchanges the JWT for a 3-minute `setup_token` in memory; pages receive only the short token.
- **Device binding** — setup tokens and challenges record IP prefix + user-agent; mismatches are treated as leakage and revoked (setup token) or rejected (challenge).
- **Replay protection** — challenges are consumed atomically (`WHERE consumed=false RETURNING`); setup tokens are deleted after successful verify/disable.
- **Encryption** — AES-256-GCM with `user_id` as AAD (v2 format; legacy `v1:` ciphertext still decrypts).
- **Lockout without TOCTOU** — failure counters and lock timestamps are incremented in a single atomic `UPDATE … RETURNING`.
- **Recovery codes** — stored as bcrypt hashes; verification samples at most 2 hashes per failed attempt to bound CPU DoS.
- **Open-redirect protection** — the `done`/`redirect` parameters are validated server-side against same-site URLs.
- **Fail-open login pre-check** — an exception in the 2FA filter never blocks login (logged instead).
- **Migration safety** — advisory-lock serialization + run-once bookkeeping prevent concurrent DDL deadlocks and duplicate timezone conversions.

## i18n

The plugin follows the project i18n standard ([docs/i18n-standard.md](../../docs/i18n-standard.md)): translation keys are English and the English bundle is the default. New UI strings must be added to both `i18n/en.yml` and `i18n/zh-CN.yml` with the same English key; missing keys fall back to the key text.

## Dependencies

| Dependency | Purpose | Required |
|------------|---------|----------|
| `pyotp` | TOTP generation/verification, provisioning URI | ✅ |
| `qrcode` | QR code rendering | ✅ |
| `bcrypt` | Recovery code hashing | ✅ |
| `cryptography` | AES-256-GCM encryption | ✅ |

## Troubleshooting

- **`POST /setup/init` returns 500** — `TOTP_MASTER_KEY` is missing or shorter than 32 characters. Configure it in `.env` and restart the service.
- **`POST /setup/init` returns 401** — the `setup_token` is missing, expired (3 min), or its device context no longer matches. Re-open the setup menu to obtain a fresh token.
- **QR code flashes and disappears / cannot be scanned** — you are running an old version. v1.2.0+ keeps the QR visible and auto-shows it on page open.
- **Admin panel nests inside the setup iframe (recursive admin panels)** — v1.2.0 fixed the Done button to navigate the top-level window instead of the iframe. Update and re-sync the plugin files.
- **Cannot complete login after enrolling** — the challenge is bound to the device that started login; retry from the same browser/network, or verify the account lock state (`Account locked` after repeated failures).
- **`TOTP_MASTER_KEY` changed and existing users fail verification** — the old secrets can no longer be decrypted. This is unrecoverable by design; keep the key stable and backed up.

## License

This plugin is part of the VeroRun project and is governed by the VeroRun project license.
