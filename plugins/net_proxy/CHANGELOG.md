# Changelog

## v1.0.0 — 2026-09-23

### Added

- Initial release: outbound network proxy and egress governance plugin.
- Egress channel management (direct / HTTP / SOCKS5) with encrypted credential storage (fail-closed, never plaintext at rest).
- Domain / plugin-source-tag based routing rules, request logging, connectivity probing and circuit breaking.
- Region tags (`cn` / `os` / `any`) and usage tags (`llm` / `search` / `crawl` / `social` / `push` / `market` / `mail` / `generic`).
- Admin panel under `/plugin/net_proxy/admin/*` (status / channels CRUD / rules CRUD / request log / probe log).
- Health checks for schema reachability, fused channels and recent success rate.

### Notes

- Tables: `proxy_channels`, `proxy_rules`, `proxy_request_log`, `proxy_probe_log`.
