# Changelog

## v1.5.4 — 2026-08-30

### Changes

- Version bump from v1.4.4

## v1.4.4 — 2026-08-30

### Changes

- i18n standard alignment: rewrite en/zh yml as source-string-as-key (i18n-standard §0.2), dedupe verification_code/test_code into "Verification Code"
- plugin.json: add label_i18n_key / title_i18n_key for menu and dashboard stats; settings_schema titles/descriptions converted to English source strings
- on_install / on_enable now call seed_plugin_translations (§4.3, idempotent)
- Wrap hardcoded user-facing errors (PluginManager not available / No valid config keys / Invalid phone number) and the Twilio message with _() (§2.1)
- Country dropdown now shows localized names (name_zh / name_en) based on UI language

## v1.4.3 — 2026-08-22

### Changes

- Version bump from v1.4.2

## v1.4.2 — 2026-08-20

### Changes

- Version bump from v1.3.2

## v1.3.1 — 2026-08-19

### Changes

- Version bump from v1.2.1

