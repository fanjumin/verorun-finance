# Changelog

## Unreleased

### Changes

- F-01/F-03/honest uninstall: read `user_profiles.meta` with the psycopg2 `%s` placeholder so a per-user opt-out actually takes effect on the main-DB channel (the swallowed `?` error silently forced the default); resolve reflexion lesson owners via `input_data.user_id` instead of the always-empty task top level; `on_uninstall` now returns False when the schema drop fails.
- F-DEP: self-contained fallback when the deployed kernel lacks `plugins._base.pii` or `agent_matrix.models.resolve_agent_roles` — auto extraction, live reflexion, sedimentation PII re-filter and the user edit endpoint no longer die with ImportError. Fallback regexes are byte-identical to the shared module; missing role resolution falls back to the `athena` core role.
- F-02: gate the auto-extract write pipeline and reflexion behind the same privacy opt-in already used by injection and sedimentation; opt-out/ownerless tasks are dropped before any curator call or write (reflexion_logs included).
- F-08: daily prompt evolution snapshots agents from `agent_matrix.slug` instead of the non-existent `public.agents.identifier`, so `prompt_metrics` and `evolution_rounds` are produced again.
- Platform (kernel): a plugin's APScheduler jobs are removed before `on_uninstall`, and an explicit False/exception now keeps the registry row with `last_error` and raises `PluginUninstallError` instead of reporting a clean uninstall with tables left behind.
- Self-heal a missing `user_profiles.meta` column; non-UUID path ids answer 404 instead of 500.

## v1.8.0 — 2026-09-25

### Changes

- Injection A/B test: deterministic per-user arm split, outcome recording on task completion (`ab_events`), and an admin report API.
- Retrieval regression eval harness plus admin endpoints (`eval_*` tables; migration `v1.8.0_abtest_eval.sql`).
- Declare the P2 A/B, eval and canary settings in plugin.json and align README; plugin version bumped to match the shipped migration set.

## v1.7.0 — 2026-09-25

### Changes

- Agent-scoped retrieval with fused vector scoring and an HNSW index migration (`v1.7.0_retrieval_index.sql`); detect pgvector columns via `udt_name` instead of `data_type`.
- Real prompt versioning with two-proportion z-test evolution suggestions and a `prompt_evolution_enabled` config gate.
- Extractor add/update/noop operations with conservative superseded semantics.
- GDPR user self-service API (view/edit/delete memories, opt-in).
- Move the shared PII guard to `plugins/_base/pii.py`; all call sites delegate to it.
- Fix the F4 embedding-dimension NameError in `migrate()` and remove the N+1 column-type probe in the extractor.
- README recalibration: remove overclaims and document known limitations.

## v1.6.0 — 2026-09-23

### Changes

- WP-E forgetting/tidying loop: soft archival plus quality-score decay (`v1.6.0_forgetting.sql`).
- Restore the full write+retrieval pipeline and break extractor/reflexion self-recursion; hard-cap curator input (field 2000 chars / payload 4000 chars) against unbounded prompt re-injection.
- Dynamic embedding provider switch and dimension resolution via the unified gateway; remove the dead `embedding_dim` config.

## v1.5.0 — 2026-08-27

### Changes

- Add knowledge sedimentation: high-value memories (fact/lesson) are collected daily into an admin review queue, then promoted into the shared knowledge_blocks (source='matrix', scope='user') on approval.
- Add memory_engine.sedimentation_queue table (migration v1.5.0_sedimentation.sql).
- Add /admin/memory/sedimentation* admin endpoints + review UI on the CogEvolution page.
- New config: enable_sedimentation, sedimentation_fact_min_confidence, sedimentation_lesson_min_rating, sedimentation_min_quality_score, sedimentation_daily_budget.

## v1.3.2 — 2026-08-22

### Changes

- Version bump from v1.3.1

## v1.3.1 — 2026-08-20

### Changes

- Version bump from v1.2.1

## v1.2.0 — 2026-08-19

### Changes

- Version bump from v1.1.0

