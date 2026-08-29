# CogEvolution (memory_engine)

Hierarchical agent memory with vector retrieval, Reflexion-based self-evolution and prompt metrics for multi-agent systems.

## Features

- **Layered Memory**: Working memory + long-term vector memory stored in an independent `memory_engine` PostgreSQL schema. Supports user, global, and agent-scoped memories.
- **Reflexion Engine**: Automatic self-reflection on failed or low-confidence tasks. Produces structured `{issue, lesson, action, rating}` records that feed back into the agent's behavior.
- **Prompt Evolution**: Daily metrics aggregation across prompt versions. Success rates, average ratings, and token usage tracked per agent — admin-approved suggestions surface only after statistical significance.
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
| `embedding_dim` | 1536 | Vector dimension for embedding model |
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
| `daily_extract_budget` | 200 | Max extraction calls per day |

## Admin Pages

- `/admin/memory` — Memory browser: search by keyword, filter by type/owner, soft-delete individual memories
- **Evolution Ring** tab — Dynamic ring visualization with round timeline player, phase drill-down panels, and agent filtering

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
