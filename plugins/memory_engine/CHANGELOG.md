# Changelog

## Unreleased

### Changes

- Hard-cap curator single-call input (field 2000 chars / payload 4000 chars) to guard against unbounded prompt re-injection regressions.

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

