# Changelog

## v1.2.0 — 2026-10-04

### Added

- Commercial bond quotes: the Tushare source now serves convertible-bond daily bars (`cb_daily`) and heads the `BOND` chain ahead of the free akshare → sa_gateway sources. Intraday frequencies are refused for bonds -- the source has none -- so a bond `60m` request degrades instead of being handed a daily bar dressed up as intraday.
- Bond reference fill: `ma_bond_ref` is populated from `cb_basic` (name / coupon / issue date / maturity / credit rating / conversion price) through the scheduled reference refresh. `issuer` stays empty because `cb_basic` does not carry it.
- License inheritance on ingest: vendor bars now carry `license_id` / `origin` / `dataset_id` into `ma_bars`, and the fetch path registers `ma_license` / `ma_dataset_registry` rows as insert-if-absent, so a manual `allow_*` change is never reset by a refresh.
- Egress gate: `data_link.check_egress` / `guard_egress` answer export / forward / llm requests against `ma_license` fail-closed -- an unregistered license id or an unreadable registry denies and is recorded in `ma_fetch_log`, while untagged (free) rows pass because they carry no restriction.
- PIT runtime: `upsert_pit_fundamentals` / `upsert_pit_consensus` / `upsert_index_membership` plus as-of readers. `system_from` is never defaulted, so a caller that omits the publication fact fails loud instead of stamping insertion time; fundamentals and consensus read on both time axes, while index membership stays valid-axis only, which is what removes survivorship bias.

### Notes

- `plugin.json` `version` `1.1.0` → `1.2.0`; `SKILL.md` `1.0.0` → `1.2.0` (it had drifted since 1.1.0).
- PIT is still not declared as a capability: no data source feeds those tables yet, and declaring one would make the manifest lie.
- The egress gate is a public API with no in-plugin caller yet -- this plugin has no export, forward or LLM egress path of its own.
- `security_level` is written as NULL (not assessed) rather than a guessed level; the 1..4 semantics are not defined anywhere in the schema.

## v1.1.0 — 2026-10-04

### Added

- Commercial Tushare source (`adapters/tushare_ma.py`, `TushareMaProvider`) for futures/options, placed at the head of the `FUTURE` / `OPTION` chains ahead of the free akshare → sina sources.
- Intraday frequencies (`1m`/`5m`/`15m`/`30m`/`60m`) for futures/options, which the free chain refuses by design; daily/weekly/monthly continue to be served and weekly/monthly are aggregated locally.
- BYOK token reuse: the provider delegates to the `stock_analysis` Tushare client (env `TUSHARE_TOKEN` > settings page > `config.yaml`). Missing token/entitlement degrades to `ProviderUnavailable` so the chain falls back to the free sources — no fabricated data.
- Option-chain reference fetch (`opt_basic`) exposed for the upcoming persistence layer.

### Notes

- Provider only this round: no writes into `ma_option_contracts` yet (deferred), and Greeks are not computed or zero-filled (no commercial greeks source wired).
- `plugin.json` `version` `1.0.0` → `1.1.0`; `tushare>=1.4.0` added to `python_dependencies`.

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
