# Changelog

## v1.0.0 — 2026-10-04

### Changed

- `plugin.json` / `SKILL.md` version `0.1.0` → `1.0.0` to mark the plugin as a released, functionally complete baseline. No functional change in this entry.

## v0.1.0 — 2026-10-01

### Added

- Initial release: multi-asset (futures / options / funds / bonds) market data plugin.
- Authoritative instrument identification (`asset_symbol.py`) aligned with ISO 10962 CFI, ISO 10383 MIC and exchange local code rules.
- Independent `multi_asset` schema (13 tables, idempotent DDL, no PG enum types): `ma_bars` plus fund / bond / future / option reference tables and `ma_fetch_log`.
- Governance skeleton tables (structure only, no read/write implementation this round) and `ma_bars` provenance columns.
- Provider chain reusing the `stock_analysis` provider contract and data gateway; per-asset dual-source failover with cooldown.
- Trading-day assignment (night session → next trading day), scheduled reference-data refresh (`multi_asset_ref_refresh`) and health checks.
