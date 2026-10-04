# CogEvolution (memory_engine)

Hierarchical agent memory with vector retrieval, Reflexion-based self-evolution and prompt metrics for multi-agent systems.

## Features

- **Long-term Memory**: Vector memory stored in an independent `memory_engine` PostgreSQL schema. Supports user, global, and agent-scoped memories.
- **Reflexion Engine**: Automatic self-reflection on failed or low-confidence tasks. Produces structured `{issue, lesson, action, rating}` records that feed back into the agent's behavior.
- **Prompt Evolution**: Daily metrics aggregation across prompt versions. Success rates, average ratings, and token usage tracked per agent — suggestions compare the latest two prompt versions per agent with a two-proportion z-test and only surface when both versions have ≥10 samples; applying any change remains a manual admin decision.
- **Evolution Ring**: Interactive SVG visualization of the full evolution lifecycle. Multi-round playback with play/pause/prev/next controls; click any phase node to drill down into its artifacts (memories, reflexions, prompt versions).
- **Privacy-first**: Per-user Opt-in toggle (config default + per-user override via `user_profiles.meta`), PII filtering (passwords, API keys, phone numbers, ID cards), independent schema isolation.
- **Graceful Degradation**: pgvector optional — falls back to keyword search when vector extension is unavailable. No feature loss.

## Installation

1. Ensure PostgreSQL 16+ with pgvector extension is available (optional; keyword fallback works without it).
2. Place this plugin directory under `plugins/memory_engine/` in your system.
3. Enable the plugin from Admin → Plugins.
4. The plugin auto-runs schema migrations on first enable. No manual SQL required.

## Configuration

All settings are available from the plugin settings page in the admin panel:

| Setting | Default | Description |
|---------|---------|-------------|
| `top_k` | 5 | Number of memories retrieved per query |
| `max_memory_block_len` | 1200 | Max characters in the injected memory block |
| `enable_auto_extract` | true | Automatically extract memories from completed tasks |
| `enable_reflexion` | true | Trigger reflexion on failures and low-confidence tasks |
| `reflexion_min_confidence` | 0.4 | Confidence threshold below which reflexion triggers |
| `reflexion_failure_only` | true | Only reflect on failed tasks |
| `retention_days` | 365 | Days before auto-archiving old memories |
| `max_memories_per_owner` | 500 | Hard cap per user (oldest/lowest-quality archived first) |
| `allow_global_memory` | false | Enable admin-curated cross-user memory |
| `memory_opt_in_default` | true | Default consent for new users |
| `prompt_evolution_enabled` | false | Enable prompt evolution suggestions |
| `daily_extract_budget` | 200 | Max extraction calls per day |
| `enable_sedimentation` | true | Promote high-value memories into the shared knowledge base after admin review |
| `sedimentation_fact_min_confidence` | 0.9 | Min confidence for fact memories to be sedimented |
| `sedimentation_lesson_min_rating` | 4 | Min reflexion rating (1-5) for lesson memories to be sedimented |
| `sedimentation_min_quality_score` | 0.7 | Min memory quality score for sedimentation |
| `sedimentation_daily_budget` | 50 | Max candidates scanned per daily sedimentation run |
| `abtest_enabled` | false | Split users 50/50 (deterministic hash) and record task outcomes to measure injection benefit |
| `abtest_control_pct` | 50 | Control group percent (0–100) |

## Admin Pages

- `/admin/memory` — Memory browser: search by keyword, filter by type/owner, soft-delete individual memories
- **Evolution Ring** tab — Dynamic ring visualization with round timeline player, phase drill-down panels, and agent filtering

## A/B Testing & Evaluation

Two opt-in capabilities for measuring whether memory injection actually helps. Both are off by default.

### Injection A/B Test

- **Split key is the user, not the session.** `sha256('abtest|<user_id>') % 100 < abtest_control_pct` → `control`, otherwise `treatment`. The `before_prompt_resolve` hook only receives `user_id`, `agent_id`, `user_query` and `task_type`, so there is no `session_id`/`task_id` available to split on. A given user therefore always lands in the same arm (no flicker); assignment is random across users. Aggregate per user × day.
- **Behaviour:** control-arm users get no memory block at all; treatment-arm users are injected as usual. With `abtest_enabled=false` (default) everything behaves exactly like a plain install.
- **Outcome recording:** every `agent.task.completed` writes one row to `ab_events` (`arm`, `injected`, `block_len`, `failed`, `confidence`, `retries`) — including control-arm rows with `injected=false`. Token usage is joined read-only from `agent_token_logs` by `task_id` at report time. Keys on `(task_id, user_id)`; failures are swallowed so the task pipeline is never affected.
- **Report:** `GET /admin/memory/abtest/report?days=14&min_sample=30` — per-arm sample size, success rate, average confidence, average tokens, plus a two-proportion z-test (significance at ±1.96) and a verdict of `memory_helps` / `memory_hurts` / `no_significant_difference` / `insufficient`.

### Retrieval Regression Evaluation

A zero-LLM-cost harness that drives the retrieval layer directly (`MemoryRetriever`) and scores it against a fixed case set — so prompt or retrieval changes can be regression-tested without paying for inference.

| Method | Path | Description |
|---|---|---|
| GET | `/admin/memory/eval/cases?active=true` | List evaluation cases. |
| POST | `/admin/memory/eval/cases` | Add a case (`name`, `owner_id`, `query`; optional `agent_id`, `expected_memory_id`, `expected_keywords`). |
| POST | `/admin/memory/eval/run` | Run every active case and archive the result into `eval_runs`. |
| GET | `/admin/memory/eval/runs` | Recent runs (`case_count`, `hit_at_k`, `mrr`) for before/after comparison. |

Scoring uses top-5 retrieval (`_TOP_K = 5`): **hit@5** is 1 when the expected memory id or any expected keyword appears in the ranked results, and **MRR** is the mean of `1 / rank` of the first such hit.

**Known item:** a retrieval run bumps `hit_count` / `last_hit_at` on matched rows (the retriever always tracks hits), so evaluation runs are visible in the "Injections Today" statistic. The evaluation metrics themselves are unaffected.

**Known item:** `ab_events.block_len` records the injected block length observed by the worker that served the prompt. With multiple gunicorn workers a completion event may be handled by a worker whose cached value is `0` or stale, so a treatment row can occasionally show `block_len = 0`. The report only aggregates at arm level, where this does not change the verdict.

## Architecture

All business logic resides in `plugins/memory_engine/`. The AI engine kernel requires three low-impact patches:

1. **Patch A** — `UnifiedLLM.get_embedding()`: Embedding capability for vector search
2. **Patch B** — `EventName.AGENT_TASK_COMPLETED`: Task completion event emitted after each agent run
3. **Patch C** — `before_prompt_resolve` filter hook: Injection point for memory blocks into the system prompt

The plugin does not hold or call LLMs directly — all inference goes through the kernel's `AgentRunner` with model policies and budget gates. Vectorization uses the kernel's embedding capability. Cost and model selection remain under administrator control.

### Data Flow

```
Task completed → EventBus → MemoryExtractor (async) → memories table
New session → PromptResolver → before_prompt_resolve filter → MemoryRetriever → injected block
Failure detected → ReflexionService (async) → reflexion_logs + lesson memory
Daily cron → PromptEvolutionService → prompt_metrics aggregation + round archival
Admin → Evolution Ring → GET /admin/memory/graph → SVG visualization
```

### Database

All plugin data lives in the `memory_engine` PostgreSQL schema, completely isolated from the main system schema:

| Table | Purpose |
|-------|---------|
| `memories` | Layered agent memories (preference, fact, decision, correction, lesson) |
| `reflexion_logs` | Structured self-reflection records |
| `prompt_metrics` | Per-version prompt performance metrics |
| `evolution_rounds` | Evolution lifecycle rounds for the Evolution Ring |
| `schema_version` | Migration tracking |

Uninstalling drops the entire `memory_engine` schema with `CASCADE` — zero residue.

### Built-in Agent

- **Memory Curator** (`memory_curator`): A tier-`cheap` sub-agent that handles memory extraction and reflexion. Uses structured JSON output contracts. Prompt file: `agents/memory_curator_prompt.md`.

## Known Limitations

- **success_rate is based on reflexion samples, not all tasks**: Reflexion only records failed/low-confidence tasks by default (`reflexion_failure_only=true`), so `success_rate` in `prompt_metrics` means "non-failure rate among reflected tasks", not full-task success rate.
- **`prompt_evolution_enabled` gate**: Daily prompt-metrics aggregation and evolution round archival only run when this plugin config boolean is explicitly `true`. Previous versions never read this flag and always aggregated — an opt-in policy change.
- **Agent-scoped retrieval**: User memories are filtered by `agent_id` on retrieval (S10). Memories with `agent_id=''` are treated as universal and injected for all agents; `owner_type='global'` memories are always included regardless of agent.
- **pgvector absence**: When the `vector` extension is unavailable, `embedding` column falls back to `TEXT` and all vector paths degrade to keyword search.
