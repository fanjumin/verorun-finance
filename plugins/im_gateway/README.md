# IM Gateway (im_gateway)

> Version: **v3.1.0** — responsibility-focused build plus IM foundation hardening (8 outbound channels · fail-closed inbound verification · unified HTTP client).

## Overview

IM Gateway is VeroRun's **instant-messaging channel gateway** plugin. Using an Adapter pattern it centralizes channel credentials, connection tests, and outbound message/media delivery, and it also ships a complete **web third-party login** (OAuth federated login) capability. The plugin uses a dedicated PostgreSQL schema, `im_gateway`.

v3.0.0 completes a responsibility realignment: **social content publishing / social OAuth accounts / token refresh scheduling** moved back to the `social_push` plugin, and **mini-program login / developer API keys / mini-program developer accounts** moved back to the `mini_app_builder` plugin. IM Gateway now owns only two things: *IM outbound channels* and *web third-party login*.

v3.1.0 hardens the IM foundation: a unified `http_client` (5 s connect / 15 s read timeout, SSRF private-network blocking, size-capped downloads, in-process token cache), new **Slack / Discord** adapters, real official APIs for DingTalk and QQ, and inbound webhooks switched to **native per-platform signature verification with fail-closed behavior**.

## Capability Status (honest)

| Channel | Key | Outbound text | Connection test | Media push | Inbound verify | Notes |
|---------|-----|:---:|:---:|:---:|:---:|-------|
| Feishu | `feishu` | ✅ | ✅ real API | ✅ | shared secret | enabled by default |
| WeCom | `wecom` | ✅ | ✅ real API | ✅ (bot webhook / app message) | shared secret | enabled by default |
| DingTalk | `dingtalk` | ✅ | ✅ real API | — | shared secret | outbound via OAPI work notification |
| QQ | `qq` | ✅ | ✅ real API | — | ✅ Ed25519 | official Bot API (`api.sgroup.qq.com`) |
| Telegram | `telegram` | ✅ | ✅ real API | — | ✅ secret_token | configured on demand |
| LINE | `line` | ✅ | ✅ real API | — | ✅ HMAC-SHA256 | configured on demand |
| Slack | `slack` | ✅ | ✅ real API | — | ✅ v0 HMAC-SHA256 | new in v3.1.0 |
| Discord | `discord` | ✅ | ✅ real API | — | ✅ Ed25519 | new in v3.1.0 |

> Once registered in `adapters/__init__.py`, Slack / Discord **appear automatically** as admin cards (the overview is driven by the adapter registry; fields are rendered from `get_config_fields()`), with no template or table changes needed.

### Outbound HTTP & SSRF Protection

All outbound requests go through `http_client.py`:

- **Unified timeouts**: 5 s connect / 15 s read — no more bare `urlopen` without a timeout.
- **SSRF blocking**: `safe_fetch()` is used only for **externally supplied URLs** (e.g. media `file_url`) — it rejects private / loopback / link-local / cloud-metadata (`169.254.169.254`) / reserved ranges (including IPv6 and IPv4-mapped), allows only http/https on ports 80/443, forbids embedded credentials, re-validates every redirect hop, and caps downloads at 10 MB.
- **Token cache**: Feishu `tenant_access_token` and WeCom / DingTalk access tokens are cached per app with an in-process TTL, so sends do not re-exchange tokens every time.
- Hard-coded platform API hosts (`open.feishu.cn`, etc.) get unified timeouts only — no private-network blocking.

## Features

- **Unified multi-channel management**: centralized credentials, secret fields auto-masked; blank values on update keep the old secret.
- **Adapter pattern**: `BaseIMAdapter` defines the contract; add a channel by implementing and registering one subclass.
- **Unified outbound facade**: `gateway.send_message()` / `gateway.test()` / `gateway.list_channels()`.
- **Cross-worker rate limiting**: a PG `im_channel_rate_events` counter enforces 20 calls / channel / 60 s across gunicorn workers.
- **Media push**: `push_media()` for the core media library (currently Feishu / WeCom).
- **Unified outbound HTTP**: `http_client.py` centralizes timeouts / disables auto-redirects / caches tokens; media downloads go through `safe_fetch()` (SSRF blocking + size cap).
- **Fail-closed inbound verification**: `/webhook/<channel>` performs **native per-platform verification** for telegram / line / slack / discord / qq; a missing key or mismatched signature returns 401 — never "allow when unconfigured".
- **Core event integration**: subscribes to `stock.alert.triggered` and pushes only when that alert opted into the `im` channel.
- **Web third-party login loop**: authorize → callback → code-for-token → federated user binding → JWT, reusing the auth-center login core.
- **Dedicated schema**: `im_gateway`, 5 tables (see below); uninstall clears only IM runtime tables and keeps login data.

## Architecture

```
Admin UI admin_imgateway.html (2 tabs: Instant Messaging / Third-party Login)
        │
        ▼
Routing layer (5 blueprints)
  routes.py             /admin/channels/*        IM channel CRUD + connection tests
  routes_overview.py    /admin/channels/overview  aggregated overview (card UI data)
  routes_login.py       /admin/channels/login/*   login provider credential admin (Plan A)
  routes_third_login.py /api/v1/oauth/*           web OAuth login loop (Plan B)
  routes_webhook.py     /webhook/<channel>        inbound webhook (native verify, fail-closed → normalize → dispatch)
        │
        ▼
Inbound verification webhook_signing.py
  telegram(secret_token) / line(HMAC) / slack(v0 HMAC) / discord(Ed25519) / qq(Ed25519)
        │
        ▼
Adapter layer adapters/
  base.py (BaseIMAdapter)
  feishu / wecom / telegram / line / slack / discord / dingtalk / qq (all real outbound)
        │
        ▼
Outbound foundation http_client.py (unified timeouts · SSRF blocking · TTL token cache)
        │
        ▼
Data layer models.py — PG schema: im_gateway
  channel_configs        IM channel credentials (config_json)
  im_channel_rate_events      cross-worker rate-limit counter
  login_providers        third-party login provider config
  login_user_bindings    federated identity ↔ main-user binding
  oauth_login_states     OAuth state (CSRF, one-time, 10-min expiry)
```

## Directory Layout

```
im_gateway/
├── plugin.json                 metadata (v3.1.0; capabilities: im.channel.list / im.message.send / im.message.receive / im.account.bind)
├── __init__.py                 entry: blueprint registration, event subscription, dashboard, uninstall
├── models.py                   schema / 5 tables, default seeds, idempotent migration from main DB
├── gateway.py                  GatewayFacade: IM outbound facade + PG rate limit (social publish removed)
├── http_client.py              unified outbound HTTP (5s/15s timeouts · SSRF blocking · size cap · TTL token cache)
├── webhook_signing.py          native inbound verification (telegram/line/slack/discord/qq, fail-closed)
├── routes.py                   IM channel CRUD + connection tests
├── routes_overview.py          aggregated overview endpoint
├── routes_webhook.py           inbound webhook entry (verify → normalize → dispatch)
├── routes_login.py             login provider admin (Plan A)
├── routes_third_login.py       web OAuth login loop (Plan B)
├── events.py                   inbound event normalize / dispatch
├── adapters/                   adapter registry and per-channel implementations
│   ├── __init__.py / base.py
│   ├── feishu.py / wecom.py / telegram.py / line.py
│   └── slack.py / discord.py / dingtalk.py / qq.py
├── login/                      web login provider registry + code→token exchange (stdlib urllib only)
│   ├── providers.py / exchange.py
├── i18n/                       en.yml / zh-CN.yml
└── templates/admin_imgateway.html
```

## Installation & Enablement

1. The plugin ships under `plugins/im_gateway`; place it in `plugins/`.
2. Enable it on the admin "Plugin Management" page. On enable it idempotently creates the schema and 5 tables, seeds default channels, and attempts to migrate historical channel config from the main DB.
3. Open "System → IM Gateway", configure Feishu / WeCom (and others), and run a connection test.

Default channel seeds:

| Channel | Key | Enabled by default |
|---------|-----|:---:|
| Feishu | `feishu` | yes |
| WeCom | `wecom` | yes |
| QQ | `qq` | no |
| DingTalk | `dingtalk` | no |

Telegram / LINE are created on demand.

## API Endpoints

### IM channel admin (admin required)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/channels/` | list channels (secrets masked) |
| GET | `/admin/channels/<channel>` | channel detail (incl. env fallback hints) |
| PUT | `/admin/channels/<channel>` | save / update (masked values do not overwrite) |
| POST | `/admin/channels/<channel>/test` | connection test |
| GET | `/admin/channels/overview` | aggregated overview (`im` / `login` sections) |

### Login provider admin (Plan A, admin required)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/channels/login/providers` | provider catalog + saved config (secret masked) |
| POST | `/admin/channels/login/providers/save` | save credentials (blank client_secret keeps old value) |
| POST | `/admin/channels/login/providers/enable` | enable / disable a provider |
| GET | `/admin/channels/login/authorize/<provider>` | build an authorize URL (for testing) |

Built-in provider catalog: `wechat` (open-platform QR) / `qq` / `weibo` / `github` / `google` — see `login/providers.py`.

### Web third-party login loop (Plan B, public)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/oauth/<provider>/login` | start authorization (state persisted for CSRF) → redirect to provider |
| GET | `/api/v1/oauth/<provider>/callback` | callback → exchange code → bind user → JWT → sso_token cookie → redirect to main site |

Notes:

- `login/exchange.py` uses **stdlib urllib only** — no new pip dependency — and returns `{openid, nickname, avatar, email}`.
- Federated identity is stored in `login_user_bindings`; the main `users` schema is not extended. A main user is get-or-created by unique `username` (`<provider>_<first12(md5(openid))>`).
- State is stored in `oauth_login_states`, consumed once, and expires after 10 minutes.
- The login core reuses auth-center `session_service.issue_auth_session` (unified issuance with 2FA / disabled-account checks); 2FA redirects to the main site with `?needs_2fa=1&challenge_token=...`.
- Register `<origin>/api/v1/oauth/<provider>/callback` in each provider's app console.

### Inbound Webhook (native verification · fail-closed)

`POST /webhook/<channel>`: first **verify** (`webhook_signing.verify`), then normalize (`events.normalize_event`) and dispatch to subscribers (`events.dispatch_event`).

Supported channels: `telegram` / `line` / `slack` / `discord` / `qq` (anything else → 404). Mechanism and key source per platform:

| Channel | Header | Key source (`channel_configs`) |
|---------|--------|------|
| telegram | `X-Telegram-Bot-Api-Secret-Token` constant-time compare | `secret_token` (falls back to env `IM_GATEWAY_WEBHOOK_SECRET`) |
| line | `X-Line-Signature` = base64(HMAC-SHA256(body)) | `channel_secret` |
| slack | `X-Slack-Signature` = `v0=…`, timestamp within ±5 min | `signing_secret` |
| discord | `X-Signature-Ed25519` over (timestamp+body) | `application_public_key` |
| qq | `X-Signature-Ed25519` over (timestamp+body) | `bot_secret` (falls back to `client_secret`) |

**Fail-closed**: a missing key or a mismatched signature always returns **401** — never "allow when unconfigured". Protocol handshakes run after verification: Discord `PING(type=1) → PONG`, QQ `op=13` callback-URL validation returns an Ed25519-signed response, and Telegram returns an empty 200.

## Public Python API

```python
from plugins.im_gateway.gateway import gateway

gateway.send_message(channel='telegram', to='<chat_id>', content='Hello')  # IM outbound
gateway.test(channel='feishu', data={...})                                 # connection test
gateway.list_channels()                                                    # enumerate channels
```

> The social multi-publish `gateway.publish()` and OAuth `connect()` were removed in v3.0.0. Use the `social_push` plugin for those capabilities.

## Core Event Integration

Via `get_event_handlers()` the plugin subscribes to `stock.alert.triggered`. It pushes formatted text to every *enabled* IM channel only when that alert's `channels` includes `im`; misconfigured channels or send failures are logged and never break the event chain.

## Extension Guide: Add an IM Adapter

1. Create a file under `adapters/` (e.g. `slack.py`), subclass `BaseIMAdapter`, and implement `get_config_fields()` / `test_connection()` / `send()` (override `push_media()` if media is needed).
2. Register it in `_ADAPTERS` in `adapters/__init__.py`.
3. If a default row is needed, add it to `_SEED_CHANNELS` in `models.py`.
4. Add the adapter's error strings to `i18n` (keep en / zh-CN keysets identical).

## Uninstall Behavior

`on_uninstall` drops only the two IM runtime tables (`im_channel_rate_events`, `channel_configs`) and **explicitly keeps** the three login tables (`login_providers` / `login_user_bindings` / `oauth_login_states`) — federated login bindings are user assets and must not be wiped when IM channels are removed.

## Known Limitations & Roadmap

- **Credentials are still stored in plaintext**: `channel_configs.config_json` and `login_providers.client_secret` (legacy design); credential encryption remains planned.
- **QQ Ed25519 key derivation**: implemented per the official convention (`private key = sha256(bot_secret) → Ed25519`; offline vector tests pass). Re-verify against a live callback before production use.
- **QQ proactive messages** (without `msg_id`) require platform authorization; unauthorized errors are surfaced as-is rather than being disguised.
- **WeCom media**: group bots do not support file/video/audio messages, so unsupported `make_media` types are honestly downgraded to a markdown download link — **no fabricated `media_id`**.
- **`events.subscribe` currently has no business consumer**: inbound events can be verified, normalized, and dispatched, but no built-in subscriber exists yet (chatbot and others subscribe on demand).
- **No live end-to-end verification**: this batch was validated statically and with offline vectors only; real sending / webhook registration must be self-tested with real credentials.

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.
