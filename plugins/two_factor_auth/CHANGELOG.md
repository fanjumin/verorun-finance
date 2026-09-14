# Changelog

## v1.2.0 — 2026-08-29

### Fixes

- fix(2fa): navigate the setup page Done button to the top-level window — stops the admin panel nesting recursively inside the setup iframe (bound-to `/admin/` redirect caused an infinite iframe admin loop after enrolling)

### Changes

- feat(2fa): rebuild setup page to Authenticator-style flow (auto-show QR on open, keep QR visible, scan / manual-key switch with copy, step wizard 1→2→3, 30s code countdown, recovery-code copy-all & print)
- feat(2fa): make /setup/init idempotent (repeat init returns the stored pending key, supporting page reload without losing QR)
- feat(2fa): add i18n entries for new setup UI (en / zh-CN)
- docs(2fa): add English & Chinese READMEs including a full user manual (README.md / README_CN.md), bump version to 1.2.0

## v1.1.0 — 2026-08-28

### Changes

- Version bump from v1.0.0
- fix(two_factor_auth): apply security audit fixes F-01 through F-10
- fix(two_factor_auth): serialize migrations, prevent timezone leak, fix setup-page auth
- feat(plugin): add 2FA login precheck filter registration
- refactor(2fa): make two-factor auth a pure plugin
