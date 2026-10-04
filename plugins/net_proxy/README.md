# Net Proxy (net_proxy)

## Overview

Net Proxy is VeroRun's unified outbound network proxy and egress governance plugin. It centralizes egress channel management (direct / HTTP proxy / SOCKS5 proxy), domain-based routing rules, request logging, connectivity probing, and circuit-breaking — giving operators a single control plane for all plugin outbound traffic.

## Features

- **Egress channel management**: create / update / delete proxy channels (HTTP / HTTPS / SOCKS5), with encrypted credential storage (fail-closed — never stored plaintext)
- **Domain-based routing rules**: route traffic by target domain or plugin source tag to specific channels
- **Circuit breaker**: consecutive failures trip a channel open; automatic recovery after cooldown
- **Connectivity probing**: scheduled health checks against channel endpoints, with probe result logging
- **Request logging**: every proxied request logged with source, target, latency, and outcome
- **Region awareness**: channels tagged by region (`cn` / `os` / `any`) for routing decisions
- **Usage tags**: channels tagged by purpose (`llm` / `search` / `crawl` / `social` / `push` / `market` / `mail` / `generic`)
- **Dashboard stats**: channel count, today's requests, probe health ratio, fused channels
- **Health checks**: schema connectivity, fused channel count, 24h success rate

## Architecture

```
Admin UI
    │
    ▼
Routes (/plugin/net_proxy/admin/*)
  status / channels CRUD / rules CRUD / request log / probe log
    │
    ├── channels.py     channel validation + proxy URL construction (pure functions)
    ├── crypto.py       credential encryption (fail-closed, never plaintext at rest)
    ├── rules.py        egress rule resolution (domain → channel lookup)
    ├── egress.py       outbound request execution + blocked network enforcement
    ├── fuse.py         circuit breaker logic
    ├── region_profile.py  current region profile resolution
    ├── health.py       schema / fused-channel / success-rate health checks
    └── scheduler.py    periodic probe + log retention cleanup
    │
    ▼
Data layer — PG schema: net_proxy
  proxy_channels / proxy_rules / proxy_probe_log / proxy_request_log
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `default_timeout_s` | 10 | Default outbound request timeout (seconds) |
| `probe_interval_minutes` | 5 | Channel connectivity probe interval |
| `probe_target` | (empty) | Probe target URL; empty = probe the channel endpoint itself |
| `fuse_threshold` | 3 | Consecutive failures before a channel is fused |
| `fuse_cooldown_minutes` | 10 | Fuse cooldown duration before recovery |
| `log_retention_days` | 30 | Request log retention days |
| `default_policy` | `DIRECT` | Default egress policy when no rule matches (`DIRECT` / `PROXY` / `BLOCK`) |

## API Endpoints

> All endpoints require admin JWT. Response envelope: `{success: bool, data: ..., error: ...}`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/plugin/net_proxy/admin/status` | Egress governance overview (channels, health, today's stats, fuse count) |
| GET | `/plugin/net_proxy/admin/channels` | List channels (credentials masked) |
| POST | `/plugin/net_proxy/admin/channels` | Create a channel (password encrypted before storage) |
| PUT | `/plugin/net_proxy/admin/channels/<id>` | Update a channel (empty password = keep existing) |
| DELETE | `/plugin/net_proxy/admin/channels/<id>` | Delete a channel |
| GET | `/plugin/net_proxy/admin/rules` | List routing rules |
| POST | `/plugin/net_proxy/admin/rules` | Create a routing rule |
| PUT | `/plugin/net_proxy/admin/rules/<id>` | Update a routing rule |
| DELETE | `/plugin/net_proxy/admin/rules/<id>` | Delete a routing rule |
| GET | `/plugin/net_proxy/admin/log/requests` | Request log (paginated, max 200 rows) |
| GET | `/plugin/net_proxy/admin/log/probes` | Probe history log |

## Python Dependencies

Required: none (stdlib only)
Optional: `PySocks>=1.7.0` (SOCKS5 proxy support)

## Permissions

- `api:read`, `api:write`, `network:request`, `routes`, `scheduler`, `health`

## Hooks

- **Provides**: `net_proxy/get_status`, `net_proxy/get_channels`, `net_proxy/get_request_log`
- **Listens**: none

## Security

- **Credential encryption**: channel passwords are encrypted at rest via `crypto.py`; if the encryption key is unavailable, the plugin fails closed (never stores plaintext) and returns HTTP 503
- **Private-network blocking**: egress requests enforce blocked network ranges (cloud metadata, loopback, link-local)
- **Admin-only**: all management endpoints require admin JWT validation
- **Fail-closed**: encryption unavailable → reject credential writes; fuse open → no traffic through a failed channel

## Uninstall

Drops the `net_proxy` schema and all tables — zero residue.

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.
