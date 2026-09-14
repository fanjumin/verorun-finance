# VeroRun — Enterprise Multi-Core AI Operating System

[![Version](https://img.shields.io/badge/version-0.61.1-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-EULA%20v1.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)]()
[![Plugins](https://img.shields.io/badge/plugins-36-orange.svg)]()

**VeroRun is a multi-core AI operating system that makes intelligent execution trustworthy, verifiable, and traceable — driven by core capabilities including agent collaboration, knowledge retrieval, content generation, and process orchestration, deployed on customer-owned servers.**

The engine core provides five intelligent execution primitives: **Multi-Agent collaboration** (role matrix + task decomposition + parallel dispatch + self-evaluating retry + Reflexion memory evolution), **knowledge retrieval** (RAG hybrid search: pgvector vectors + pg_trgm keywords + RRF fusion + hierarchical memory), **process orchestration** (DAG workflow engine + event-bus triggers + Cron scheduling), **model access** (UnifiedLLM gateway, provider-agnostic, transparent routing + automatic failover), and **asset protection** (VeroGuard integrity verification + offline HMAC-SHA256 licensing). Primitives are triggered via HTTP API, Cron, or the event bus; the Master Agent decomposes instructions into subtasks, dispatches them to specialized Agents for parallel execution, and aggregates the results — producing PPTX/DOCX/Markdown documents, images, structured data, knowledge-base embeddings, or alert notifications. Business capabilities are assembled on top of the kernel as plugins — a plugin is an application, a plugin is an industry.

---

## Key Features

- **Orchestrable Multi-Role Agent Matrix**: Athena (master) + 10 sub-roles, role division + task-decomposition orchestration, auto-registration of extended agents.
- **Four-Stage Discussion Protocol (Agent Discussion v2.0)**: Planner → Reviewer → Revise → Decider, separating generation from review, intercepting plans before they land.
- **Dynamic Prompt System**: database-driven `PromptResolver`, four-layer assembly + scenario differentiation + multi-version management.
- **Cognitive Evolution Engine (CogEvolution)**: RAG vector retrieval, layered memory, Reflexion learning, Prompt Evolution, forming a "memory → reflection → optimization → behavioral evolution" loop.
- **Visual Workflow Engine**: DAG node orchestration, Cron scheduling, tiered worker pools — a general execution carrier for any process.
- **Multi-Provider LLM Gateway (UnifiedLLM)**: provider-agnostic unified API, 7 native + 2 dynamically resolved providers, transparent model substitution, automatic failover, key management, budget gate, 4-level quota.
- **VeroGuard Guard Layer**: health monitoring + integrity verification + encrypted heartbeat, dual-process mutual protection, client-side asset protection; Ed25519-signed releases, integrity manifests, and source watermarks guard the supply chain.
- **Plugin Ecosystem**: 36 built-in plugins carry any business form, full lifecycle management, plugin marketplace, licensing engine.

Kernel design principle: **business semantics are declared entirely by plugins** — adding a business capability is equivalent to assembling a plugin, keeping the kernel stable.

---

## Architecture

### Engine Base vs. Application Layer

> **The engine base defines "how it runs"; application plugins define "what runs".**

```text
┌──────────────────────────────────────────────────────────────┐
│ Application Layer  Plugin apps: knowledge · content · commerce│
│                   · communications · ops …                    │
│                   36 built-in plugins; any business via plugins│
├──────────────────────────────────────────────────────────────┤
│ Engine Base       Multi-role AI Agent Matrix + Discussion     │
│                   Knowledge memory (vector) · Workflow · PromptResolver │
├──────────────────────────────────────────────────────────────┤
│ Runtime Layer     UnifiedLLM gateway · Plugin manager · Themes │
├──────────────────────────────────────────────────────────────┤
│ Guard Layer       VeroGuard unified daemon (health/integrity/ │
│                   heartbeat)                                  │
└──────────────────────────────────────────────────────────────┘
```

### Service Topology

| Port | Domain | systemd Unit | App | Responsibility |
|---|---|---|---|---|
| 8081 | main domain | `verorun-main` | `auth_server` | Main site, login, captcha proxy |
| 8083 | `platform.*` | `verorun-auth` | `main_site` (Platform Console) | User console, subscription |
| 8084 | `admin.*` | `verorun-admin` | `admin.app` | Admin panel, Agent matrix, automation, CMS |
| 8085 | — | `verorun-health` | `health_service.app` | Internal health check endpoint |
| — | — | `verorun-guardian` | `veroguard.guardian` | Unified daemon (health + integrity + heartbeat) |

> Note: `auth-center/` is a **shared code library** (models / services / routes) imported by each service; port 8083 actually runs the `main_site` app.

### Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, Flask, Gunicorn |
| Database | PostgreSQL (production, with pgvector) / SQLite (dev fallback) |
| Cache | Redis |
| Reverse proxy | Nginx + Let's Encrypt |
| Workflow editor | React 18.3.1 + React Flow |
| Visualization | Chart.js, ECharts, Quill.js |
| Daemon build | Nuitka (standalone binary) |
| Image generation | FLUX.1-pro (SiliconFlow), Tongyi Wanxiang |

---

## Quick Start

### Deployment Types

| `INSTALL_TYPE` | Scenario | Repository | Domain/HTTPS |
|---|---|---|---|
| `website` | Production site (domain + HTTPS) | `verorun-pro` | Yes |
| `professional` | Enterprise intranet (LAN access) | `verorun-pro` | No |
| `development` | Development (full source, requires SSH key) | `verorun-code` | No |
| `educational` | Education edition (requires ED deploy code) | `verorun-edu` | No |

### One-command Deployment (Ubuntu 22.04 / 24.04)

```bash
# Production site (with domain)
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh \
  | sudo env INSTALL_TYPE=website bash -s -- install your-domain.com

# Enterprise intranet (IP-based access)
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh \
  | sudo env INSTALL_TYPE=professional bash
```

Region selection: `--region=cn` (Mainland China, defaults to `api.verorun.cn`) / `--region=global` (international, defaults to `api.verorun.com`).

The install mode runs automatically: system deps → PostgreSQL → user/dirs → clone → venv → `.env` → systemd services → Nginx → start → **DB migration + seed (automatic)**. SSL requires manually running certbot after DNS resolves.

### Docker

```bash
# PG_PASSWORD and MINI_APP_PG_PASSWORD must be set in .env first
docker compose up -d          # exposes port 80, single container (supervisord orchestrates all services)
```

### Local Development

```bash
pip install -r requirements.txt
cp .env .env.local            # create local config from existing .env
flask run --port 8081
```

---

## Engine Base

### AI Engine — Multi-Role Agent Matrix (21 Roles)

VeroRun hands complex tasks to a group of specialized, reviewable Agent roles: the master decomposes tasks, sub-roles each do their part, the reviewer challenges the plan, and the decider signs off on the conclusion.

| Role | Slug | Type | Model | Responsibility |
|---|---|---|---|---|
| Athena | `athena` | master | openai/gpt-4o | Task decomposition, orchestration, reporting, escalation, system management |
| Content | `content` | sub | siliconflow/DeepSeek-V3 | Content creation, publishing, analytics, ads, social media |
| Builder | `builder` | sub | siliconflow/DeepSeek-V3 | Site building, themes, domains, page design |
| Finance | `finance` | sub | gemini/gemini-2.5-flash | Plans, subscriptions, billing, orders, payment, invoices, rewards |
| Ops | `ops` | sub | deepseek/deepseek-v4-flash | Deployment, health checks, alerts, automation, cloud provisioning |
| Service | `service` | sub | kimi/moonshot-v1-32k | Customer service, FAQ, tickets, notifications, verification |
| Vision | `vision` | sub | zhipu/glm-4v-plus | Image analysis, OCR, diagram interpretation, visual QA |
| Creative | `creative` | sub | siliconflow/FLUX.1-pro | Text-to-image, creative visual design |
| Business | `business` | sub | deepseek/deepseek-v4-flash | Business analysis, products, orders, supply chain, reviews, wishlist |
| Veroscholar | `veroscholar` | sub | deepseek/deepseek-v4-flash | Academic research: literature search, review, experiment design, paper writing |
| Stock Analyst | `stock_analyst` | sub | deepseek/deepseek-v4-flash | Financial analysis: market data, indicators, strategy research |

**Extended Research Roles** (veroscholar discovery & finance research sub-roles):

| Role | Slug | Purpose |
|---|---|---|
| Literature Scout | `lit_scout` | Literature discovery and retrieval for hypothesis evidence |
| Method Architect | `method_architect` | Experiment / methodology design |
| Thesis Smith | `thesis_smith` | Paper writing and assembly |
| Peer Auditor | `peer_auditor` | Independent review and audit of research output |
| Research Planner | `rs_planner` | Investment-research planning |
| Fundamental Analyst | `rs_fundamental` | Financial statement & fundamental analysis |
| Quant Analyst | `rs_quant` | Quant / factor & price-volume analysis |
| Risk Officer | `rs_risk` | Risk / counter-argument (devil's advocate) |
| Portfolio Manager | `rs_pm` | Synthesis and portfolio decision |
| Compliance Officer | `rs_compliance` | Research compliance & audit gatekeeping |

**Extended Agents** (auto-registered via `sub_*_prompt.md`): Supply Chain, Chatbot, Automation, Health Check, User, CMS, Cleaner.

**Execution Mechanism**: sub-tasks are dispatched in parallel via ThreadPoolExecutor (≤5 workers) with a 300s per-task timeout; agents self-evaluate (rule-based pre-check + LLM structural review) with a confidence threshold of 0.7 and retry (default 2 attempts) below threshold; LLM response caching is enabled at temperature=0 with a 3600s TTL.

### Four-Stage Discussion Protocol (Agent Discussion v2.0)

The collaboration protocol is composed of three roles — Planner, Reviewer, Decider — and executes in 4 rounds:

1. **Planner** produces an initial execution plan (plan_v1).
2. **Reviewer** reviews it, outputting issues and revised_steps.
3. **Planner** revises into plan_v2 based on the review.
4. **Decider** makes the final approve / reject decision with reasoning.

Separating generation from review transplants the discipline of "plan review + sign-off" from engineering organizations into the LLM workflow, trading structure for quality and traceability.

### Dynamic Prompt System

The dynamic prompt system uses a **database-driven, tag-matching, chained-assembly** architecture. At runtime, `PromptResolver` assembles the System Prompt in real time from the task context; prompts are stored in the `agent_prompts` table, with `agent_matrix/prompts/*.md` as initialization seeds (migrated by `seed_prompts.py`).

**Four-Layer Assembly**:

1. **Role base**: the role's base prompt from the default binding in `agent_prompt_bindings`.
2. **Global safety rules**: rules with `prompt_type='rule'` and `domain='general'`.
3. **Scenario templates**: scenario prompts whose `task_triggers` exactly match the `task_type`.
4. **Mode enhancements**: tool or scenario prompts matched by `mode` tag / mode binding.

**Data Model**: `agent_prompts` (version, is_active, priority, tags, task_triggers) + `agent_prompt_bindings`; the `prompts_db.html` admin page offers visual management, and multiple versions of the same slug are supported with switch and rollback.

**Toggle & Fallback**: controlled by `system_config.prompt_resolver_enabled`; active when the key holds a truthy value, with safe fallback to legacy mode on toggle-off or read error (reads `agent_matrix.system_prompt`, with path-traversal protection). Three-layer fallback: toggle off → legacy; assembly exception → legacy; no match after four-layer lookup → original `system_prompt` logic. Follows the "availability-first" principle.

**Integration with Cognitive Evolution**: dynamic prompts are injected with memory through the kernel `before_prompt_resolve` filter chain, and support Prompt Evolution's per-version metric aggregation and one-click application of new versions.

### Knowledge Base & Memory Engine (CogEvolution)

Since v0.56.4, `memory_engine` has been upgraded into a cognitive evolution engine forming a "memory → reflection → optimization → behavioral evolution" loop:

- **Vector retrieval (RAG)**: retrieves context from a document knowledge base; AI Q&A includes source citations.
- **Reflexion**: triggered on task failure or low confidence; extracts failure context → root-cause analysis → generates structured reflection → persists to long-term memory, auto-retrieved for later similar tasks to avoid repeating mistakes.
- **Prompt Evolution**: aggregates execution metrics per prompt version and generates optimization suggestions when statistically significant; admins apply a new version in one click. Requires explicit enablement (`prompt_evolution_enabled`).
- **Evolution-loop visualization**: a pure-SVG interactive component rendering decision paths, reflection trigger points, and prompt-version switches in a ring topology, with replay and drill-down.
- **Layered memory**: working memory (in-process) + long-term vector memory (pgvector); supports user / global / agent scopes; privacy-first (user-level opt-in, PII auto-filtering, isolated schema); falls back to keyword retrieval when pgvector is absent.

### Project Workspace (Knowledge Retrieval in Practice)

The engine's "knowledge retrieval" primitive is realized as a project-level knowledge base by the `project_workspace` plugin:

- **Schema isolation**: each project gets an independent PostgreSQL schema with enforced `WHERE project_id=?` queries.
- **Document RAG**: supports PDF / DOCX / TXT / MD / PPTX / XLSX / CSV upload with an async pipeline (extract → chunk → embed → store).
- **Semantic retrieval**: pgvector with keyword fallback; AI Q&A includes source citations and feedback scoring.
- **Workspace assistant**: document summarization, comparison, source-traced Q&A, and content analysis.
- **RBAC**: Viewer (retrieve / ask) / Editor (upload / edit) / Owner (manage project and members).

### Veroscholar Discovery Engine

`veroscholar` (v1.11.x) extends academic research with a **hypothesis Discovery Engine** that turns an open research question into an evidence-backed, ranked hypothesis set:

- **Pipeline**: generates candidate hypotheses → recalls multi-source evidence → ranks them via an **Elo tournament** → clusters by keywords → runs three rounds of deliberation (generate / critique / evolve) → final judge ruling → persists results.
- **Constrained scope**: current output is a candidate hypothesis set + Elo ranking + deliberation trail; it does not claim structured falsifiability testing (reserved for a later phase).
- **Storage**: 6 discovery tables (`discovery_runs`, `hypotheses`, `hypothesis_votes`, `hypothesis_discussions`, `discovery_events`, `discovery_memory`) — idempotent, no pgvector dependency.
- **Execution**: synchronous path plus 7 custom DAG nodes (`veroscholar_discovery_*`) and a "Hypothesis Tournament" workflow blueprint (async execution auto-degrades to sync if the orchestrator engine is unavailable).
- **Gate**: disabled by default (`discovery_enabled=false`); when off, all discovery APIs return 403 and DAG nodes are side-effect-free.
- **LLM**: all model calls route through `agent_matrix` tier model resolution + UnifiedLLM (budgeted per module).

### stock_analysis v2

The financial-analysis plugin (`stock_analysis`) shipped v2.0.0 on top of three architectural hardfixes:

- **Fine-grained permissions**: replaces blanket admin checks with a three-tier model — `stock.read` / `stock.write` / `stock.admin` — enforced on every endpoint; JWT carries the user's permission list.
- **MCP upgrade**: Provider access supports MCP **Streamable HTTP** transport while remaining backward-compatible with stdio.
- **Explicit adjustment (复权) semantics**: an adjustment-factor engine with a corporate-action table (`sa_corp_action`, `sa_adj_factor`) removes the prior look-ahead/declared-close ambiguity.

The v2 plugin also adds a full research stack, drawn from a dedicated implementation plan: `secmaster` symbol resolution, a **Provider v2 contract** (`base_v2.py` / `FetchResult` with provenance), FMP / Polygon / terminal / user-supplied data sources, an evidence-bundle anti-hallucination layer, financial normalization (TTM / DuPont / Beneish M-Score / Altman Z), valuation models (DCF / reverse DCF / comps / PB-ROE), a factor lab with six backtest rules, and a compliance/audit framework (approval workflow, silence periods, audit retention).

### Visual Workflow Engine

Any task drivable by agent collaboration and process orchestration can be arranged on a visual DAG:

- **DAG orchestration**: 12 registered node types — `ai_agent`, `data_collect`, `ai_process`, `condition`, `approval`, `publish`, `notify`, `wait`, `sub_workflow`, `market_check`, `http_request`, `script`.
- **Implementation caveat**: `approval`, `sub_workflow`, and `script` currently have placeholder handlers only — confirm implementation status before use.
- **Cron scheduling**: APScheduler-based, supporting Cron / Interval / Date triggers, pause / resume, priorities (critical / high / normal / low), exponential-backoff retry, and natural-language cron parsing.
- **Tiered worker pools**: `dedicated_pool` (4 threads) + `shared_pool` (8 threads); priorities ≤ HIGH go to dedicated, otherwise to shared.

### Multi-Provider LLM Gateway (UnifiedLLM)

`UnifiedLLM` is the unified entry point for all LLM interactions:

| Capability | Description |
|---|---|
| Provider access | 7 native providers: DashScope / OpenAI / DeepSeek / OpenRouter / SiliconFlow / Gemini / Grok; GLM and Moonshot are resolved dynamically via the `provider_models` table |
| Dual addressing | via `provider_model_id` (recommended) or legacy `provider + model` |
| Client cache | 5-minute TTL, thread-safe |
| Key resolution priority | `provider_api_keys` table (encrypted) → environment vars → `system_config` table |
| Streaming | `chat_stream()` with automatic token usage tracking |
| Tool calling | `chat_with_tools()` for function-calling agents |
| Budget gate | daily token cap (default 2M) + per-minute rate limit (default 30 calls / 60s), fail-open |
| 4-level quota | priority User > Model > Module > Global |

**Orchestration**: UnifiedLLM shields provider differences from upper layers — applications all talk the same API shape while the gateway handles interface translation, model routing, and automatic failover behind the scenes. Models are addressed dynamically by capability and cost, enabling transparent substitution and automatic degradation; onboarding a new provider or switching models is transparent to business code.

---

### VeroGuard Guard Layer

Unifies health monitoring, code-integrity verification, and encrypted heartbeat into a single process across 7 core modules (health / integrity / fingerprint / runtime / communicator / executor / self_protect):

| Channel | Interval | Mechanism |
|---|---|---|
| Health watchdog | 30s | Service health checks, tiered recovery (restart → rollback), webhook alerts |
| Integrity verification | 300s | Per-file SHA256 comparison against an encrypted manifest (AES-GCM) |
| Heartbeat report | 300s | AES-256-GCM + HMAC-SHA256 signing + TLS 1.3, 5-minute anti-replay window |

- **Self-protection**: dual-process (`guardian` monitors business services, `self_protect` monitors the guardian) with pipe / pidfile heartbeat and automatic restart on parent death.
- **Signed release chain**: Ed25519-signed release manifests + integrity manifests + official token verification; version-scope downgrade protection (`min/max_version`); build-time `build_id` source watermarks.
- **Remote commands** (6): `warn`, `lock_ai`, `lock_full`, `shutdown`, `self_destruct`, `update_config`; destructive commands (`shutdown` / `self_destruct`) additionally require a local ops-token confirmation.

---

## Plugin Ecosystem

Full lifecycle management (6 states: `UNKNOWN → INSTALLED → ENABLED → ACTIVE → DISABLED → UNINSTALLED`) plus an `ERROR` state.

### Kernel-Related Plugins

| Plugin | Related Primitive | Summary |
|---|---|---|
| `memory_engine` | Agent collaboration / Knowledge retrieval | Cognitive evolution engine: RAG + layered memory + Reflexion + Prompt Evolution |
| `project_workspace` | Knowledge retrieval | Project-level document RAG, schema isolation, semantic retrieval with citations |
| `content_factory` | Process orchestration | Multi-source collection → AI processing → review → publish, listens to `cron.tick` |
| `health_check` | Asset protection | Auto health inspection + AI Fixer fault analysis + alerts |
| `analytics` | Process orchestration | Server-side privacy-first analytics, workflow nodes: report / AI insight / alert / CSV |
| `vault` | Asset protection | Full/incremental backup, AES-256-GCM encryption, multi-target storage |

### Domain Plugins

| Domain | Plugins |
|---|---|
| Knowledge management | `chatbot`, `memory_engine`, `cogevolution_substrate`, `project_workspace`, `veroscholar` (incl. Discovery Engine) |
| Content publishing | `content_factory`, `site_builder`, `mini_app_builder`, `ads`, `social_push` |
| Business operations | `shop`, `payment`, `logistics`, `subscription`, `coupons`, `order_notify`, `reviews`, `wishlist`, `stock_analysis`, `risk_control` |
| Communications | `im_gateway`, `email`, `sms`, `oauth_config`, `verification` |
| Ops & security | `health_check`, `vault`, `captcha_embedded`, `enterprise_verify`, `two_factor_auth`, `ali_api`, `iot_hub` |
| Data & utilities | `visitor_profile`, `analytics`, `currency_converter`, `site_domains` |

**Plugin Manager**: auto-scans `plugins/` and parses `plugin.json`; dependency resolution via Kahn topological sort with cycle detection; event bus with 31 system events (thread-pool async dispatch); WordPress-style Action / Filter hooks with priority; JSON Schema Draft-07 config validation; per-plugin isolated logs (rotating 5MB × 3).

**Plugin Marketplace**: browse / search (remote API + local cache), one-click install (SHA256 integrity + Zip Slip protection), Alipay QR payment, subscriptions and coupons. Licensing: online HMAC-signed validation + offline token (HMAC-SHA256, 72h grace + 7-day validity, bound to Site ID); official releases are Ed25519-signed with integrity manifests and official-token verification (supplemented by a local revocation list), version downgrades are protected (`min/max_version`), and build artifacts carry a `build_id` source watermark.

### Custom Plugins

Plugin contract: create a directory under `plugins/`, provide `plugin.json` (metadata + dependencies + config schema) and implement `register_routes()` / hooks. The manager auto-scans and registers it, integrating via the event bus and Action/Filter hooks. Custom plugins enjoy the same capabilities as built-in ones: route mounting, workflow node registration, event subscription, and agent tool exposure.

---

## Content Generation

Content generation is one of the general capabilities carried by the engine, covering the batch production and distribution of diverse content forms such as articles, images, and marketing assets. Leveraging the same engine, content can be delivered directly to front-ends such as websites and mini programs:

- **Content production** (`content_factory`): batch generation and distribution of articles, images, and marketing assets.
- **AI site building** (`site_builder`): generates websites, themes, and pages, presenting content in site form.
- **Mini programs** (`mini_app_builder`): extends content capabilities to the mini-program front-end.

Content generation and the knowledge retrieval, process orchestration, model access, and asset protection primitives can be combined on demand by business plugins to form complete applications.

---

## Business Model & Regional Routing

**Three-stage funnel**: free distribution of the standard enterprise package and the education edition for lead generation (public `verorun-pro` and `verorun-edu` repositories) → plugin purchases, subscriptions, and commercial licenses for recurring revenue → VeroGuard protects code assets and licensing rights on the customer side. **Data-flywheel vision**: centered on domain knowledge assets, knowledge bases self-evolve through business usage, powering domain-model fine-tuning and intelligent-device training.

**Regional routing**: `VERORUN_REGION=cn` → `api.verorun.cn`; `=global` → `api.verorun.com`. All remote services (licensing / heartbeat / daemon) resolve dynamically by region, with single-URL environment-variable override.

---

## SDKs

| Package | Platform | Description |
|---|---|---|
| `@verorun/sdk-common` | Cross-platform | Auth, Chat, RAG |
| `@verorun/sdk-wechat` | WeChat | WeChat Mini Program wrapper |
| `@verorun/sdk-douyin` | Douyin | Douyin / Toutiao Mini Program wrapper |
| `@verorun/sdk-telegram` | Telegram | Bot API + WebApp |
| `@verorun/sdk-line` | LINE | LIFF + Messaging API |
| `verorun` CLI | Python / Cross-platform | Thin REST client: chat, agents, tasks, automation, plugins; JWT auth + SSE streaming (`sdks/cli/`) |

---

## Directory Structure

```text
VeroRun/
├── admin/                  # Admin panel (8084)
├── auth-center/            # Shared auth/model/services/routes (shared code library)
├── main_site/              # Platform console / user backend (8083)
├── agent_matrix/           # AI Engine: multi-agent orchestration
│   ├── roles/              # 21 role YAML definitions (master + sub + research roles)
│   ├── prompts/            # Dynamic prompt seeds (17 .md; runtime loads from agent_prompts table)
│   ├── prompt_resolver.py  # Dynamic prompt dispatching engine
│   ├── engine.py           # UnifiedLLM gateway + budget + quota
│   ├── orchestrator.py     # Task decomposition, parallel dispatch
│   └── agent_runner.py     # Self-evaluating executor
├── orchestrator/           # Visual workflow engine (DAG)
├── plugins/                # 36 built-in plugins (business form assembly)
├── plugin_manager/         # Plugin lifecycle / marketplace / licensing / regional routing
├── veroguard/              # VeroGuard guard layer (7 modules)
├── health_guardian/        # Guardian support package (with health_guardian.py entry)
├── health_service/         # Health check service (8085)
├── providers/              # Pluggable provider abstraction
├── sdks/                   # Python CLI client (mini-program JS SDKs vendored into plugins/mini_app_builder/sdks/)
├── shared/                 # Shared utilities
├── i18n/                   # Internationalization (en, zh-CN)
├── prompts/                # Kernel-level prompt assets
├── templates/ · static/ · themes/  # Jinja2 base templates, static assets, theme system
├── scripts/ · tools/       # Maintenance scripts and developer tooling
├── deploy/                 # Installation scripts, systemd units, Nginx config
├── docs/                   # Design specs, plugin standard, audit & test reports
├── auth_server.py          # Main-site entry (8081) · run_gunicorn.py / run_auth_wsgi.py launchers
├── GUIDE.md · CHANGELOG.md · VERSION
├── Dockerfile · docker-compose.yml
└── LICENSE
```

> Note: business directories such as `site_builder/` belong to the application layer carried by the engine.

---

## Codebase Metrics

Measured on the v0.60.0 working copy (2026-09-01) with a `cloc`-style single-pass scan that classifies every line of every source file as **code / comment / blank** using language-specific comment syntax. Excluded from the count: build artifacts and vendored third-party code (`__pycache__`, `node_modules`, `backups/`, `data/`, `tmp/`, hidden dependency caches such as `.stock_deps/`), and the internal engineering documents under `docs/` (plans, session handoffs, audit reports) — so the numbers reflect first-party product source only.

**Total: 229,130 code lines across 1,351 files** (plus 11,071 comment and 31,654 blank lines). Excluding prompt/documentation formats (Markdown, Text), executable code + markup ≈ **213,200 lines**.

### By Language

| Language | Code | Comment | Blank | Files |
|---|---:|---:|---:|---:|
| Python | 122,092 | 6,966 | 21,154 | 632 |
| HTML (Jinja2 templates / admin pages) | 41,786 | 242 | 2,553 | 173 |
| YAML (roles, seeds, CI, configs) | 17,366 | 317 | 429 | 106 |
| Markdown (product READMEs, prompt seeds) | 15,811 | 0 | 5,259 | 181 |
| JavaScript | 10,525 | 1,638 | 1,059 | 77 |
| CSS | 10,095 | 529 | 547 | 32 |
| JSON (plugin manifests, release manifests) | 5,546 | 0 | 28 | 61 |
| Shell (deploy/ops scripts) | 4,180 | 1,078 | 426 | 15 |
| SQL (migrations / seeds) | 1,488 | 273 | 183 | 33 |
| Other (TS/JSX/SVG/Batch/Env/Text) | 241 | 28 | 14 | 41 |

### By Module (source languages only)

| Module | Code lines | Share | Role |
|---|---:|---:|---|
| `plugins/` | 96,940 | 45.5% | Application layer — 36 plugins |
| `admin/` | 19,642 | 9.2% | Admin console (8084) |
| `auth-center/` | 16,035 | 7.5% | Shared auth/models/services/routes |
| `main_site/` | 15,704 | 7.4% | Platform console (8083) |
| `plugin_manager/` | 13,137 | 6.2% | Lifecycle / marketplace / licensing |
| `agent_matrix/` | 8,009 | 3.8% | AI engine: orchestration + UnifiedLLM + PromptResolver |
| `deploy/` | 5,180 | 2.4% | Install scripts, systemd, Nginx |
| `static/` | 4,215 | 2.0% | Front-end assets |
| `orchestrator/` | 3,664 | 1.7% | DAG workflow engine |
| `sdks/` + `veroguard/` + `tools/` + `providers/` + rest | ~7,000 | ~3.3% | CLI SDK, guard layer, tooling, providers, i18n, themes |

Kernel proper (`agent_matrix` + `orchestrator` + `plugin_manager` + `veroguard` + `providers` + `shared`) ≈ **27.1k lines ≈ 12.7%** of executable code — consistent with the "business is declared by plugins" design principle: the application layer is ~3.6× the kernel.

### Largest Plugins (source lines)

`mini_app_builder` 8,006 · `analytics` 7,845 · `ali_api` 7,281 · `vault` 6,997 · `site_builder` 6,513 · `shop` 6,449 · `health_check` 6,386 · `im_gateway` 5,675 · `subscription` 4,405 · `veroscholar` 3,793.

### Analyst Notes

- **Language profile**: Python-dominant (57.3% of executable code) with server-rendered HTML consoles (41.8k lines) — a Flask monolith-plus-plugins architecture rather than an SPA stack; front-end JS is confined to editors (React Flow) and visualization.
- **Comment density**: Python carries ~5.7% comment lines — thin for a 122k-line codebase; docstrings help, but complex subsystems (VeroGuard, licensing, workflow engine) warrant denser inline rationale.
- **Test surface**: in-repo `tests/` suites exist across core and plugins (`shared`, `plugin_manager`, `sdks/cli`, `stock_analysis`, `veroscholar`, `cogevolution_substrate`, `shop`, `subscription`, `iot_hub`), ~30+ unit / API test modules; quality assurance is additionally carried by dated regression/audit reports under `docs/`. Expanding executable CI coverage remains the highest-leverage engineering investment.

---

## Documentation

- `GUIDE.md` — installation and usage guide
- `CHANGELOG.md` — version changelog
- `agent_matrix/ARCHITECTURE.md` — Agent matrix design
- `sdks/README.md` — SDK usage
- `deploy/README.md` — deployment notes
- `plugins/memory_engine/README.md` — cognitive evolution engine
- `docs/official-api-security-spec.md` — official API security spec (token revocation + command signing)
- `docs/security-remediation-tracker.md` — copyright-protection audit remediation tracker

---

## Known Production Constraints

- The Admin service limits Gunicorn workers to 2 to avoid OOM on low-spec servers.
- SQLite mode disables `--preload` to avoid cross-process connection conflicts.
- systemd `TimeoutStartSec` must exceed `health_check.sh`'s `MAX_WAIT=180`.
- Plugin connection wrapper classes must implement commit / rollback / close to avoid idle-in-transaction pool poisoning.
- The deployment script must exclude `data/` to prevent overwriting the production database.
- Two-factor-auth login precheck is delivered by the `two_factor_auth` plugin through the `auth.before_issue_session` filter; it is active only while the plugin is enabled.

---

## License

VeroRun is distributed under a **source-available proprietary license** per the [VeroRun Base EULA v1.0](LICENSE).

**Distribution matrix**:

| Repository | Nature | Content | Governing terms |
|---|---|---|---|
| `verorun-pro` | Public | Standard enterprise package (general engine, plugins installed on demand via the marketplace) | EULA v1.0 |
| `verorun-code` | Private | Full source (all plugins, licensing components, and VeroGuard) | Private distribution terms |
| `verorun-edu` | Public | Education edition (EDU plugin allowlist, education modules only) | EULA v1.0 |

**EULA v1.0 highlights**: visible Python source may be read and modified (for customization and integration); precompiled binaries (.pyd/.so/.dll/executables) may not be decompiled, disassembled, or reverse-engineered; no redistribution, resale, or sublicensing; no use to build competing products; no removal of copyright notices, license keys, or DRM mechanisms; production commercial deployment requires a valid commercial license.

**Consistency with VeroGuard**: VeroGuard's health monitoring, integrity verification, self-protection, and remote-command capabilities are the operational enforcement of EULA §2.2 (no decompiling binaries) and §3 (no removing licensing/DRM mechanisms), guarding the license boundary and commercial assets. The EULA proprietary license makes the asset-protection mechanism legally self-consistent.

Copyright (c) 2024-2026 VeroRun AI. All rights reserved. See [LICENSE](LICENSE) for details.

---

## Local Development Tools

### Local Development Tool (not versioned)

- `tools/dev_insight/` is a **local-only** developer tool that analyzes the VeroRun architecture (system core + business plugins) and emits JSON / Markdown / HTML reports plus a token-friendly `AI_CONTEXT.md` for AI assistants.
- It is **excluded from Git** via `.git/info/exclude`; do **not** commit it or its `out/` output.
- Usage: `python tools/dev_insight/insight.py`
