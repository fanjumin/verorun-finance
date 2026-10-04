# Stock Analysis (stock_analysis)

> **Version**: v2.0.2 (see `plugin.json`; full changelog in `CHANGELOG.md`)
> **Audience**: developers / ops / auditors — architecture, module responsibilities, data flow, storage model, deployment constraints.
> For end-user capabilities, endpoint reference, and desktop contract, see [`USAGE.md`](./USAGE.md); platform skill declaration see [`SKILL.md`](./SKILL.md).

A-share research plugin for VeroRun, providing technical, valuation, sentiment, and AI-assisted analysis for **admins** and the **financial analysis Agent**. The plugin loads through the VeroRun standard lifecycle, does not bind to a specific kernel model, and stores no trading orders or user asset data.

> **Disclaimer**: outputs are **research information and risk prompts, not auto-trading instructions**. All results are subject to data delays, suspensions, missing indicators, and model error. For research reference only; **not investment advice or a return guarantee**.

---

## Capability Matrix

| Domain | Description | Key modules |
|--------|-------------|-------------|
| Technical | MA5/20/60, MACD, KDJ, RSI (Wilder), BOLL, support/resistance, technical signals | `indicators.py`, `kline_service.py` |
| Valuation | PE(TTM)/PB realtime scoring; 5-year percentile overlay when tushare authorized | `valuation.py` |
| Sentiment | Sina news headline keyword statistics with recent news samples | `stock_skill.py` |
| AI synthesis | UnifiedLLM gateway: technical + evidence chain → structured signal + Chinese report | `stock_skill.py`, `evidence.py` |
| Bull-bear debate | 4-stage Agent Discussion (bull → bear → revise → decider), async + SSE rounds | `discuss_research.py` |
| Reflexion feedback | Mistake signals fed back as failed-task events to memory_engine | `reflexion_feedback.py` |
| Scenario prompt | Pre-open / intraday / postclose scenario prompt templates by time slot | `stock_skill.py` (`scenario_task_type`) |
| Auto deep research | High-confidence symbols auto-enqueued for LLM deep research after batch | `deep_research.py`, `batch.py` |
| Evidence chain | Financial statements / money flow / news / valuation percentile packed into prompt, per-source degradation with trace | `evidence.py` |
| Market overview | SSE / SZSE Component / ChiNext realtime quotes | `gateway.py` (INDEX → tencent) |
| Watchlist & batch | Watchlist management, scheduled batch analysis, result export | `batch.py`, `routes.py` |
| Async tasks | Analysis job queue (cross-worker safe, crash recovery) | `jobs_queue.py` |
| Real-time events | SSE stream (alerts/jobs/discuss topics, heartbeat + resume) | `sse_stream.py` |
| Alerts | 6 rule types scanned every 60s + event persistence + hook dispatch | `alert_engine.py` |
| Signal realization | Signal persistence + incremental backtest, quality statistics | `signal_quality.py` |
| KB publishing | Successful analyses idempotently written to platform knowledge base | `kb_publish.py` |
| Deep research | `stock_deep_research` DAG node | `deep_research.py` |
| MCP tools | 5 fast-path tools (JSON-RPC 2.0 over stdio) | `tools/mcp_server.py` |

---

## Directory Layout

```
plugins/stock_analysis/
├── __init__.py                  # Entry: routes/Agent/scheduler/DAG registration
├── plugin.json                  # Manifest (version/menu/permissions/settings/MCP/hooks)
├── routes.py                    # Admin pages + ~80 admin APIs (rate-limited)
├── stock_skill.py               # Analysis engine (technical/valuation/sentiment/LLM) + CLI
├── gateway.py                   # Data gateway: provider routing, failover, cache, concurrency
├── providers/                   # Data source adapters (BaseProvider / BaseProviderV2)
│   ├── base.py / base_v2.py / commons.py
│   ├── sina.py / tencent.py
│   ├── tushare_provider.py / akshare_provider.py
│   ├── akshare_fundamental.py / akshare_consensus.py / akshare_macro.py
│   ├── fmp_provider.py / polygon_provider.py / terminal_provider.py / user_supplied.py
├── indicators.py                # Authoritative indicators (INDICATOR_VERSION = iv-4)
├── kline_service.py             # /api/kline contract assembly
├── valuation.py                 # PE/PB historical percentile (tushare)
├── valuation_models.py          # Valuation models (DCF and related)
├── market_calendar.py           # A-share trading calendar
├── jobs_queue.py                # Async task queue (advisory lock + crash recovery)
├── sse_stream.py                # SSE event stream
├── flow_events.py               # DAG flow-span events
├── event_runner.py              # Event dispatch runner
├── alert_engine.py              # Alert rule evaluation
├── signal_quality.py            # Signal realization backtest
├── reflexion_feedback.py        # Mistake signal → Reflexion feedback loop
├── models_sa.py                 # Independent schema stock_analysis (22 tables)
├── evidence.py                  # Evidence chain packing + structured output parsing
├── evidence_bundle.py           # Evidence bundle assembly
├── deep_research.py             # stock_deep_research DAG node
├── discuss_research.py          # Bull-bear debate (4-stage)
├── research_dag.py              # Research DAG definition
├── morning_brief.py             # Morning brief generation
├── batch.py                     # Scheduled batch analysis
├── kb.py / kb_publish.py        # Knowledge base service + publishing
├── factor_lab.py                # Factor computation
├── backtest_runner.py           # Backtest execution
├── portfolio.py                 # Portfolio analysis (VaR and related)
├── compliance.py                # Compliance engine
├── classification.py            # Sector classification (point-in-time)
├── sector_sw.py                 # Shenwan sector data
├── corporate_actions.py         # Corporate actions (dividends / splits)
├── secmaster.py                 # Security master
├── symbol_master.py             # Symbol master + search
├── tushare_client.py            # Tushare client
├── ashare_special.py            # A-share special rules
├── expectation_gap.py           # Expectation gap
├── financial_quality.py         # Financial quality scoring
├── metrics_collector.py         # Metrics collection
├── llm_cache.py                 # LLM response cache
├── crypto.py                    # Credential encryption
├── broker/                      # Paper trading (paper.py / risk.py / adapter.py)
├── config.yaml                  # Local config
├── requirements.txt             # Dependencies
├── tools/                       # mcp_server.py / gen_calendar.py / capture_baseline.py / compare_baseline.py
├── data/calendar/               # Trading calendar CSVs
├── agents/                      # stock_analysis_agent_prompt.md
├── templates/stock_analysis.html # Admin iframe page
├── i18n/                        # en.yml / zh-CN.yml
├── tests/                       # Unit + regression tests
├── SKILL.md / USAGE.md / README.md / CHANGELOG.md
```

---

## Data Source Routing

| Category | Provider order | Notes |
|----------|---------------|-------|
| `KLINE` (daily) | tushare → akshare → sina | failover chain, pruned by `supports()` probe |
| `FUNDAMENTAL` | tushare | auto-degrades without token, evidence chain logs "unavailable (reason)" |
| `MONEYFLOW` | tushare | same |
| `NEWS` | sina | sentiment keyword statistics |
| `QUOTE` (realtime) | tencent | price, change%, PE(TTM), PB, turnover |
| `INDEX` | tencent | SSE / SZSE / ChiNext |

- `data_provider` setting = preferred source override (pinned to chain head); empty = default order.
- **Circuit breaker**: consecutive failures cool down for 1800s (30 min).
- **Freshness gate**: K-line data older than 10 natural days is rejected and failover to next source.

### Two-level cache

| Layer | Implementation | Notes |
|-------|---------------|-------|
| In-process TTL | bounded dict (max 1024) | LRU eviction |
| Daily disk | gzip (DataFrame → JSON split) | valid for current day |

TTL by category (seconds): KLINE 300 / QUOTE 60 / INDEX 60 / NEWS 600 / FUNDAMENTAL 300 / MONEYFLOW 300.

---

## Storage Model

Independent schema `stock_analysis` (borrowed from shared connection pool):

| Table | Purpose |
|-------|---------|
| `sa_watchlist` | Watchlist symbols |
| `sa_analysis_run` | Batch run records |
| `sa_analysis_result` | Batch results |
| `sa_signal_log` | Signal persistence (idempotent anchor) |
| `sa_signal_realized` | Signal realization backtest |
| `sa_jobs` | Async task queue |
| `sa_alerts` | Alert rules |
| `sa_alert_events` | Alert trigger events |
| `sa_sse_events` | SSE event stream (for resume) |
| `sa_flow_spans` | DAG flow-span trace archive |
| `sa_corp_action` | Corporate actions (dividends / splits) |
| `sa_adj_factor` | Price adjustment factors |
| `sa_classification` | Point-in-time sector classification |
| `sa_sector_constituent` | Shenwan sector constituents |
| `sa_compliance_audit` | Compliance audit log |
| `sa_compliance_approval` | Compliance approvals |
| `sa_compliance_silence` | Compliance silence rules |
| `sa_paper_account` | Paper trading account |
| `sa_paper_order` | Paper trading orders |
| `sa_paper_position` | Paper trading positions |
| `sa_symbol_master` | Symbol master |
| `sa_llm_cache` | LLM response cache |

---

## Scheduled Jobs

| Job ID | Trigger | Purpose |
|--------|---------|---------|
| `stock_analysis_daily_batch` | cron Mon–Fri 15:05 | Post-close batch analysis of watchlist |
| `stock_analysis_signal_realize` | cron Mon–Fri 16:00 | Incremental signal realization backtest |
| `stock_analysis_alert_scan` | interval 60s | Alert rule periodic scan |

---

## Configuration

| Key | Type | Description |
|-----|------|-------------|
| `tushare_token` | string (password) | User's own Tushare Pro token; empty = auto fallback to free sources |
| `fmp_api_key` | string (password) | Financial Modeling Prep API key (US stock fundamentals) |
| `polygon_api_key` | string (password) | Polygon.io API key (US stock K-line/quotes/news) |
| `data_provider` | enum | Preferred source override: "" / sina / tencent / akshare / tushare / fmp / polygon |
| `data_cache_dir` | string | Disk cache directory (default `./data/cache`) |
| `auto_deep_research_on_batch` | boolean | Auto-enqueue deep research for high-confidence symbols after batch (default false) |
| `index_selection` | array | Indices shown on market overview (default: SSE / SZSE Component / ChiNext) |

**Environment overrides**: `TUSHARE_TOKEN` (highest), `STOCK_DATA_CACHE_DIR`.

Model selection, API keys, quotas, caching, and usage audit are all handled by the VeroRun `UnifiedLLM` kernel. The plugin uses `standard` tier and does not directly specify provider/model/keys.

---

## API Endpoints

Prefix `/admin/stock-analysis`. All require admin JWT. Response envelope: `{ok, data, error, meta}`.

| Method | Path | Purpose | Rate limit |
|--------|------|---------|-----------|
| GET | `/api/analyze` | Single-stock analysis (type: technical/fundamental/sentiment/llm) | 30/60s |
| GET | `/api/signal` | Quick technical signal | 60/60s |
| GET | `/api/market` | Market overview | — |
| GET/POST/DELETE | `/api/watchlist` | Watchlist CRUD | various |
| POST | `/api/batch/run` | Run batch analysis | 5/60s |
| GET | `/api/batch/results` | Batch results | — |
| GET | `/api/batch/export` | Export results JSON | 10/60s |
| GET | `/api/fundamental-detail` | Financial statements detail | 30/60s |
| GET | `/api/moneyflow` | Money flow | 30/60s |
| GET | `/api/signal-quality` | Signal quality stats | — |
| POST | `/api/signal-realize` | Trigger realization backtest | 3/60s |
| GET | `/api/kline` | Daily K-line + indicators | 60/60s |
| GET | `/api/quotes` | Batch realtime quotes (≤50 symbols) | 120/60s |
| POST | `/api/jobs` | Create analysis job | 10/60s |
| GET | `/api/jobs/<job_id>` | Job status + result | 60/60s |
| POST | `/api/discuss` | Bull-bear debate (async) | 10/60s |
| GET/POST/DELETE | `/api/alerts` | Alert rules CRUD | various |
| GET | `/api/alerts/events` | Alert events | — |
| GET | `/api/events` | SSE stream (topics, Last-Event-ID) | — |

### Additional Endpoints

**Admin page / search / status**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Admin iframe page |
| GET | `/api/search` | Symbol search |
| GET | `/api/constants` | Constants / enum catalog |
| GET | `/api/gateway/health` | Data gateway health |
| GET | `/api/system/throughput` | System throughput metrics |
| GET | `/api/deps/status` | Runtime dependency status |
| POST | `/api/deps/install` | Install a runtime dependency |

**Quotes & market data**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/quote/depth` | Order-book depth |
| GET | `/api/quote/ticks` | Tick trades |
| GET | `/api/macro` | Macro data |
| GET | `/api/macro/catalog` | Macro data catalog |
| GET | `/api/consensus` | Analyst consensus |

**Signals & jobs**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/signals/today` | Today's signals |
| GET | `/api/signals/daily` | Daily signal series |
| GET | `/api/signals/summary` | Signal summary stats |
| GET | `/api/jobs` | List jobs |
| GET | `/api/flow/spans` | DAG flow-span trace |
| GET | `/api/agents/runtime` | Agent runtime status |

**Research (async)**

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/research/report` | Enqueue research report job |
| GET | `/api/research/report/<job_id>` | Research report job status |

**Sectors**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sectors` | Sector ranking |
| POST | `/api/sectors/ingest` | Ingest sector classification |
| GET | `/api/sectors/valuation` | Sector valuation |

**Backtest & portfolio**

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/backtest` | Run backtest |
| GET | `/api/backtest/factors` | Factor list |
| POST | `/api/portfolio/analyze` | Portfolio analysis |

**Compliance**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/compliance/audit` | Compliance audit log |
| GET | `/api/compliance/audit/export` | Export audit log |
| POST | `/api/compliance/check` | Run compliance check |
| GET | `/api/compliance/approvals` | Approval list |
| GET | `/api/compliance/approvals/<int:approval_id>` | Approval detail |
| POST | `/api/compliance/approvals` | Create approval |
| POST | `/api/compliance/approvals/<int:approval_id>/review` | Review approval |
| GET | `/api/compliance/silence` | Silence rules |
| POST | `/api/compliance/silence` | Create silence rule |

**Knowledge base**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/kb/docs` | KB document list |
| POST | `/api/kb/upload` | Upload KB document |
| GET | `/api/kb/docs/<doc_id>/status` | KB doc processing status |
| DELETE | `/api/kb/docs/<doc_id>` | Delete KB doc |
| GET | `/api/kb/search` | KB search |
| POST | `/api/kb/qa` | KB question answering |
| GET | `/api/kb/stats` | KB statistics |

**Paper trading**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/trade/account` | Paper account |
| GET | `/api/trade/positions` | Positions |
| GET | `/api/trade/orders` | Orders |
| POST | `/api/trade/order` | Place order |
| POST | `/api/trade/reset` | Reset account |

**Alerts (extra)**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/alerts/schema` | Alert rule schema |

**Providers, settings & ontology**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/providers` | Provider list |
| PUT | `/api/providers/credentials` | Update provider credentials |
| POST | `/api/providers/test` | Test provider |
| GET | `/api/settings/data-sources` | Data source settings |
| POST | `/api/settings/data-sources` | Save data source settings |
| POST | `/api/settings/data-sources/test` | Test data source |
| GET | `/api/settings/indices` | Index selection |
| POST | `/api/settings/indices` | Save index selection |
| GET | `/api/ontology/graph` | Ontology graph |

---

## MCP Tools

Stdio transport MCP server (`name = stock`):

```json
{ "command": "python", "args": ["plugins/stock_analysis/tools/mcp_server.py"], "transport": "stdio" }
```

5 fast-path tools: `get_quote`, `get_kline`, `get_technical_signal`, `get_fundamental_digest`, `market_overview`.

---

## Python API / CLI

```python
from stock_skill import StockAnalysisSkill
skill = StockAnalysisSkill()
result = skill.analyze("600519", analysis_type="llm", months=6)
print(result.to_text())
```

```bash
python stock_skill.py 600519                    # full analysis
python stock_skill.py 600519 --type technical   # technical only
python stock_skill.py 600519 --format json      # text / json / signal
python stock_skill.py --market                   # market overview
```

---

## Compliance & Risk Disclosure

- **All paths** carry risk disclaimers: "technical indicators do not constitute investment advice".
- **Confidence** is a heuristic signal strength clamped to [0,1], **not** statistical confidence.
- **Evidence chain trace**: each sub-source failure logs "unavailable (reason)" to prevent model hallucination.
- **No SSRF**: all outbound URLs are hardcoded to public data sources; user input is query parameters only.
- **No dangerous execution**: only `deps/install` uses subprocess (whitelisted akshare pip install, admin-confirmed).

---

## Dependencies

Required: `pyyaml`, `pandas`, `requests`, `tushare>=1.4.0`, `akshare>=1.14.0`
Optional: `fmp_api_key` / `polygon_api_key` for US stock data

Python 3.11+, VeroRun >= 0.10.0

---

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.

*This plugin is a research tool. Data and model outputs may be wrong; all trading decisions are the user's own risk.*
