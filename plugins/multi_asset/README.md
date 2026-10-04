# Multi-Asset Data (multi_asset)

Futures / options / funds / bonds — a unified **data ingestion + modeling + storage** plugin, positioned as the **asset coverage + data governance base**.

It is a **same-domain complement** to `stock_analysis`, not a second data layer: instrument identification reuses `asset_symbol.py`, and data fetching reuses `stock_analysis`'s `BaseProviderV2` contract and `DataGateway`. On top of that, this plugin owns its storage layer and bakes **license and security-level tags into the data structure**.

> One-liner positioning: **stocks belong to `stock_analysis`; non-equity assets belong to `multi_asset`; both share the same kernel contract.
> `multi_asset` additionally owns the persistence and provenance layer — every bar carries `license_id` / `security_level` / `origin` / `dataset_id`.**
>
> Per GB/T 42775-2023 ("data obtained externally must not be classified below the provider's level"): user-imported licensed data remains bound by its original license in the warehouse — source and license must be inherited as-is, not become "free upon ingestion".

## 1. Why it exists

`stock_analysis`'s secmaster / provider routing only recognizes 6-digit stock codes: when `market != GLOBAL`, secmaster narrows the scope. Questions like "what is `SA605`?" or "is `519xxx` a Shanghai bond or a Shenzhen B-share?" have no answer in the stock domain. `multi_asset` answers them with **one authoritative identification module** and persists results into an **independent schema** without polluting stock-domain tables.

## 2. Directory Layout

```
plugins/multi_asset/
├── plugin.json                 # Manifest (permissions / hooks / settings / menu / capabilities)
├── __init__.py                 # MultiAssetPlugin (lifecycle + cross-plugin API)
├── asset_symbol.py             # ★ Authoritative instrument identification (CFI / MIC / exchange rules)
├── asset_data.py               # data/*.json lazy loader (keeps CJK literals out of .py)
├── models.py                   # Independent schema DDL / upsert / query / fetch audit
├── trade_calendar.py           # Trading day assignment (night session → next trading day)
├── data_link.py                # Orchestration: resolve → fetch → persist → audit
├── events.py                   # Emit asset.data.ready (hook registry channel)
├── ref_sync.py                 # Reference data sync (main contracts / ETF list)
├── routes.py                   # Flask Blueprint /admin/multi-asset
├── adapters/
│   ├── base.py                 # AssetProviderBase (aligned to BaseProviderV2 contract)
│   ├── providers.py            # akshare / sina / sa_gateway three real sources
│   └── __init__.py             # Registry + per-asset dual-source chain + cooldown failover
├── data/                       # JSON data assets (vendor column names, Chinese variety names)
├── i18n/{en.yml,zh-CN.yml}     # Key-consistent translations
├── templates/multi_asset.html  # Standalone iframe page
├── docs/role-integration.md    # Finance-edition role orchestration integration
├── SKILL.md / README.md / README_CN.md
└── tests/                      # Acceptance tests (V-01…V-14)
```

## 3. Instrument Identification Rules

| Standard | Purpose |
|----------|---------|
| ISO 10962:2021 CFI | Asset class first letter: `E` equity / `D` debt / `C` fund / `F` futures / `O` options |
| ISO 10383 MIC | Exchange Market Identifier Code (GFEX official = `XGFE`, not `XGEF`) |
| ISO 6166 ISIN | Code reservation (not generated without paid data source) |
| Exchange local code rules | See table below |

| Exchange | Contract code rule | Example |
|----------|-------------------|---------|
| CZCE | **Uppercase** + 3-digit month (no century digit) | `SA605`, `TA605` |
| SHFE/INE/DCE/GFEX | **Lowercase** + 4-digit (year last digit + month) | `rb2610`, `sc2612`, `m2609` |
| CFFEX | **Uppercase** + 4-digit | `IF2603`, `T2603` |
| Continuous contract | Variety + `0` | `RB0`, `SA0` |

**Case is a discriminator**: `parse_future_symbol()` preserves original case to distinguish exchanges and variety→exchange mapping — it never does `upper()` first (this was the P0-1 flaw in the initial design).

6-digit numeric codes use a **longest-prefix segment table** (`parse_cn_code`): Shanghai `600/601/603/605`, STAR `688/689`, Shenzhen `000/001/002/003`, ChiNext `300/301/302`, BSE `43x/83x/87x/88x/92x`, Shanghai B `900`, Shenzhen B `200`, Shanghai fund `5xx`, Shenzhen fund `159/15x/16x/18x`, Shanghai bond `019/018/010/110/111/113…`, Shenzhen bond `112/123/127/128…`.

## 4. Data Sources & Failover Chain

| Asset | Chain (failover order) | Notes |
|-------|----------------------|-------|
| Futures | `akshare` → `sina` | akshare `futures_zh_daily_sina`; backup direct to Sina futures daily JSONP |
| Options | `akshare` → `sina` | akshare `option_hist_{shfe,dce,czce,gfex}` (probe by exchange) |
| Funds | `akshare` → `sa_gateway` | ETF `fund_etf_hist_em` / LOF `fund_lof_hist_em` / OTC NAV `fund_open_fund_info_em` |
| Bonds | `akshare` → `sa_gateway` | `bond_zh_hs_daily` (auto prefix `sh`/`sz`) |

- **Cooldown**: same (asset, source) 3 consecutive failures → cool down 300s (same semantics as stock_analysis gateway).
- Setting `data_provider` non-empty pins that source to chain head.
- When `stock_analysis` is absent, `CONTRACT_AVAILABLE=False` — all sources raise `ProviderUnavailable`, health check explicitly reports unavailable (never pretends the data source is healthy).

## 5. HTTP API

All under `/admin/multi-asset`, unified contract `{ok, data, error, meta}`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | iframe workbench page (platform injects `?token=`) |
| GET | `/api/constants` | Asset types / exchanges / MIC / frequencies / source chains |
| GET | `/api/resolve?symbol=` | Instrument normalization (type, exchange, MIC, storage key) |
| GET | `/api/bars?symbol=&freq=&datalen=&persist=` | Bars/NAV series (fetch → persist → audit) |
| GET | `/api/quote?symbol=` | Quote snapshot |
| GET | `/api/profile?symbol=` | Instrument profile |
| GET | `/api/search?q=` | Variety search + reference table search |
| GET | `/api/storage` | Table sizes + recent fetch audit |
| GET | `/api/health` | Source chain / contract / cooldown status |

Auth: JWT (`Authorization: Bearer` or `?token=`), permission `multi_asset.read`; 401 / 403 / 429 semantics align with `stock_analysis`. Futures and options responses force `risk_disclosure` and `suitability: professional_only` in `meta`.

## 6. Data Model

Independent schema `multi_asset` (`SET search_path TO multi_asset, public`), all DDL idempotent (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`), **no PG enum types** (`CREATE TYPE ... AS ENUM` has no `IF NOT EXISTS` and will fail on rerun) — asset type uses `VARCHAR(8) + CHECK` for equivalent semantics.

| Table | PK / Unique key | Description |
|-------|----------------|-------------|
| `ma_bars` | `(asset_type, symbol, exchange, freq, trade_date, bar_time)` | The system's **only** bar/NAV series table |
| `ma_fund_ref` | `(code, exchange)` | Fund reference (name, type, exchange) |
| `ma_bond_ref` | `(code, exchange)` | Bond reference (coupon, maturity, rating) |
| `ma_future_contracts` | `(symbol, exchange)` | Contract specs (multiplier, tick, last trading day, main flag) |
| `ma_option_contracts` | `(symbol, exchange)` | Option contracts (type, strike, expiry, underlying) |
| `ma_option_greeks` | `(symbol, trade_date)` | Greeks (Delta/Gamma/Vega/Theta/Rho) |
| `ma_fetch_log` | `id` | Fetch audit (source, provenance, success/fail, degradation reason) |

### 6.1 Governance skeleton tables (structure only, no read/write implementation this round)

6 tables created in one pass on 2026-10-01, **structure only, no business code**. Rationale: table structure is an irreversible decision; changing it later is an order of magnitude more expensive. Since `ma_bars` had no real data at the time, adding columns/tables was a pure structural change — once data flows in, source and license can never be retroactively populated.

| Table | Unique key | Description |
|-------|-----------|-------------|
| `ma_license` | `license_id` | License ledger. `allow_export` / `allow_forward` / `allow_llm` **default all 0 = strictest** (user license cannot be verified at import time) |
| `ma_dataset_registry` | `dataset_id` | Dataset registry (source nature, license, level, incremental watermark) |
| `ma_pit_fundamentals` | `(symbol, exchange, report_period, metric, valid_from, system_from)` | Financials PIT bi-temporal |
| `ma_pit_consensus` | `(symbol, exchange, forecast_period, metric, valid_from, system_from)` | Consensus PIT bi-temporal |
| `ma_index_membership` | `(index_code, symbol, valid_from)` | Index membership history (with `valid_to`, supports unbiased backtest) |
| `ma_quality_report` | none (append-only stream) | Quality check report landing |

**`ma_bars` also adds 5 columns**: `license_id` / `security_level` (levels 1–4, GB/T 42775-2023) / `origin` / `dataset_id` / `ingested_at`. Division of labor with the existing `source` column: `source` = **fetch channel name** (akshare / sina); `origin` = **data source nature** (exchange / legal_disclosure / public_feed / vendor / user_file).

**Trading day assignment**: daily/weekly/monthly bars use the data source's own trade date; intraday bars are computed by `trade_calendar.assign_trade_date()` — night session (≥21:00) belongs to the **next** trading day, day session belongs to the current day; CFFEX index futures have no night session.

## 7. Scheduled Jobs & Health Checks

- Job `multi_asset_ref_refresh`: Mon–Fri 17:30, sync main contracts and ETF list (best-effort; source unavailable logs only).
- Health checks: `multi_asset_db` (schema reachable), `multi_asset_sources` (per-asset source chain ≥ 2 and contract available). Cold-start empty reference tables are considered healthy.

## 8. Dependencies

- Hard dependency: `stock_analysis >= 2.0.1` (provider contract + DataGateway)
- Python: `pyyaml`, `pandas`, `akshare >= 1.14.0`
- No mandatory external API key: free sources work out of the box; paid sources degrade per chain with audit logging

## 9. Known Boundaries (Honest Disclosure)

1. Free sources have no SLA; fields may change at any time. If data cannot be fetched, it errors + logs — no fabrication.
2. Option history depends on akshare's exchange-specific endpoints; coverage varies with upstream. May be empty without paid sources.
3. Intraday frequency is not faked from daily bars. When no native intraday source exists, the API returns 503 instead of misaligned data.
4. The `sa_gateway` source only serves stock-type codes; funds/bonds use it as a **fallback** and usually won't reach chain head.
5. **Governance capabilities not yet implemented**: the 6 tables in §6.1 are structure-only. Egress gates (export / logging / LLM context), PIT as-of queries, and validation rule sets are all unimplemented. Therefore `capabilities` deliberately does **not** declare `data.ingest` / `data.pit` / `data.quality` / `data.classification` — declaring unimplemented capabilities would make the manifest a lie.

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.
