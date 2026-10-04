# NeuralFlow (neural_flow)

## Overview

NeuralFlow is VeroRun's platform-level AI dataflow observability base — the backend for the NeuralHub dashboard. It provides spans, Domain Profiles, and cross-domain replay for visualizing how data flows through the platform's AI agents.

The visualization engine is industry-agnostic: all domain-specific rendering is declared via **Domain Profiles** (`domain.yaml` or built-in profiles), so adding a new industry domain requires zero changes to the rendering engine. The stock domain (`stock_analysis`) is the first data source.

## Features

- **Spans**: every agent task, LLM call, and plugin lifecycle event becomes a span, archived in the `nf_flow_spans` table (30-day retention)
- **Domain Profiles**: declarative pipeline stages, decision routes, arches, and color mapping per industry domain — the rendering engine stays generic
- **Incremental collector**: cursor-based incremental poll of platform tables (`agent_token_logs` → LLM-domain span), with advisory-lock mutual exclusion across workers
- **Real-time SSE**: reuses the existing `stock_analysis` SSE single connection (`topic=flow`) — no new concurrent connection gate
- **Event-driven spans**: subscribes to `agent.task.completed` and plugin lifecycle events to emit agent/system-domain spans
- **Profile registry**: built-in profiles (`platform`, `stock`) + external profiles scanned from other plugins' `domain.yaml`; schema validation failures are rejected with logged errors (never block other domains)

## Architecture

```
NeuralHub Dashboard (frontend)
    │
    ├── Real-time:  SSE via stock_analysis /api/events (topic=flow)
    │               SDK dual-write → sa_sse_events (stock_analysis-owned table)
    │
    └── Archive:    GET /admin/neural-flow/api/spans
                    nf_flow_spans (own schema, 30-day retention)
    │
    ▼
Collector (interval job, default 5s)
  incremental cursor poll → agent_token_logs → llm-domain spans
  advisory lock (0x6E464C57) — single worker executes across multi-worker
    │
    ▼
EventBus subscribers
  agent.task.completed → agent-domain span
  plugin.installed/enabled/disabled → system-domain span + profile refresh
    │
    ▼
Domain Profile Registry (profile_registry.py)
  builtin: platform / stock
  external: other plugins' domain.yaml (schema-validated, rejected on failure)
```

## Data Model

Independent PostgreSQL schema `neural_flow`:

| Table | Purpose |
|-------|---------|
| `nf_flow_spans` | Archived spans (domain, trace_id, entity JSONB, payload JSONB, source, source_id) |

Indexes: `(domain, created_at)`, `(trace_id)`, unique `(source, source_id)` for collection idempotency.

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `collector_interval_seconds` | 5 | Incremental cursor poll interval for platform tables |
| `span_retention_days` | 30 | `nf_flow_spans` archive retention before daily prune |

## API Endpoints

> All endpoints require admin JWT. Response envelope: `{ok: bool, data: ..., error: ...}`.
> Prefix: `/admin/neural-flow`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/profiles` | Domain Profile registry (including rejected profiles with error details) |
| GET | `/api/spans` | Span replay query (`from`, `to`, `domain`, `trace_id`, `limit` ≤ 2000) |
| GET | `/api/events` | SSE real-time stream (`topics=flow`, `Last-Event-ID` resume) |

### SSE Concurrency

- Default max concurrent SSE connections: **2** (env `NF_SSE_MAX_CONNECTIONS`, range 1–16)
- Gate full → HTTP 503 + `Retry-After: 10` (no queueing)
- Outbox unavailable → HTTP 503 + `Retry-After: 60` (rejected before taking a slot)

## Python Dependencies

Required: none
Optional: `pyyaml` (external Domain Profile scanning; without it only built-in profiles are available)

## Permissions

- `routes`, `scheduler`, `events`

## Hooks

- **Provides**: none
- **Listens**: `agent.task.completed`, `plugin.installed`, `plugin.enabled`, `plugin.disabled`

## Compatible Editions

- All editions (`compatible_editions: []` — no restriction)

## Uninstall

Drops the `neural_flow` schema and `nf_flow_spans` table — zero residue. If foreign objects exist in the schema, the drop is refused (leaves residue rather than deleting foreign data).

## License

This plugin is part of the VeroRun platform and follows its unified license agreement.
