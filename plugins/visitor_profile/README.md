# Visitor Profile Engine (visitor_profile)

## Overview

Visitor Profile Engine builds AI-powered behavioral profiles of site visitors. It listens to `visitor.activity` events, batches them, and uses an LLM sub-agent (`profiler`) to extract semantic profiles stored with pgvector embeddings. Extracted personas are injected into the prompt resolution pipeline via the `before_prompt_resolve` filter for dynamic personalization.

## Features

- **Semantic extraction**: LLM agent extracts visitor intents, interests, and sentiment
- **pgvector storage**: profiles embedded for similarity search (`semantic_search_top_k`)
- **Dynamic persona injection**: `before_prompt_resolve` hook feeds personas into prompts
- **PII filtering**: `pii_filter_enabled` strips personal identifiable information
- **Hooks**: provides `before_prompt_resolve`; listens `visitor.activity`

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `retention_days` | `365` | Profile retention window |
| `max_events_per_visitor_per_day` | `200` | Per-visitor event cap |
| `profile_extraction_enabled` | `true` | Toggle LLM extraction |
| `pii_filter_enabled` | `true` | PII scrubbing |
| `embedding_model` | `text-embedding-3-small` | Embedding model |
| `extraction_batch_size` | `5` | Events per extraction batch |
| `extraction_max_events` | `20` | Max events per extraction run |

## Recommendations

Works best with `analytics` (event source) and `chatbot` (personalized conversations).
