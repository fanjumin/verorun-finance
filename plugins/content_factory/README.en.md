# Content Factory (content_factory)

## Overview

Content Factory is VeroRun's content production and management hub, providing an end-to-end content pipeline: multi-source collection, AI processing, review workflow, multi-channel publishing, Skill push, and static page generation. The plugin uses a dedicated PostgreSQL schema (`content_factory`) with 5 core business tables and schedules automated collection through a `cron.tick` listener.

The plugin collects content from RSS and other sources, uses DashScope (Qwen) for AI processing (summarization, rewriting, keyword extraction), provides a full review workflow (draft -> submit -> approve/reject -> publish), and can push processed content to the CMS article system, social media platforms, and the Skill knowledge base.

## Features

- **Multi-source Collection**: RSS source collection with configurable intervals, keyword filters, and max crawl volume
- **AI Processing**: Batch AI processing based on Qwen, including summaries, content rewriting, and layout optimization
- **AI Formatting & Cover Images**: AI-driven HTML layout repair and AI cover image generation (Tongyi Wanxiang)
- **Review Workflow**: Full draft -> submit -> approve/reject -> publish state machine
- **Multi-channel Publishing**: Internal CMS publishing and social media distribution (via social_push plugin)
- **Skill Push**: Converts processed content into Agent Skills and pushes them to target agents
- **Static Page Generation**: Generates static HTML pages (articles, categories, documentation index)
- **Knowledge Base Push**: Pushes processed content into the knowledge base system
- **Scheduled Collection**: Automatic collection driven by the `cron.tick` endpoint with an external cron
- **Dedicated Database**: PostgreSQL schema `content_factory` with 5 core business tables

## Architecture

```
draft --> submit_review --> review --> approve --> approved --> publish --> published
  ^                      |           |                                      |
  |                      v           v                                      |
  +--- back_to_draft ----+ rejected  +--- back_to_draft -------------------+
```

The plugin is organized into routing (`/admin/content-factory/*`), services (`services/ai_processor.py`, `services/collectors/rss_collector.py`, `services/skill_pusher.py`), and a data layer (`content_factory` schema: `content_sources`, `raw_contents`, `processed_contents`, `content_tasks`, `skill_pushes`).

## Installation & Enablement

### Prerequisites

- VeroRun platform version >= 0.10.0
- DashScope API Key (for AI processing and cover images)
- PostgreSQL database

### Install Steps

1. Place the `content_factory` directory under `plugins/`
2. Make sure `enabled` is `true` in `plugin.json`
3. Restart the application — the plugin auto-creates the PostgreSQL schema `content_factory` and initializes the 5 core tables
4. Configure the plugin in Admin > "AI & Content" > "Content Factory"

### Scheduled Collection

The plugin listens to the `cron.tick` Hook. Configure an external cron to call:

```
POST /admin/content-factory/cron/tick
Header: X-Cron-Secret: <your-cron-secret>
```

Set the environment variable `CRON_SECRET` to enable authentication.

## Configuration

| Config Key | Type | Default | Description |
|--------|------|--------|------|
| `dashscope_text_key` | string | "" | Qwen API Key for AI content processing |
| `max_items_per_run` | integer | 10 | Max items per crawl run |
| `skip_review` | boolean | false | Skip manual review, AI-processed content goes straight to approved |
| `auto_publish` | boolean | false | Auto-publish after AI processing |

## Key API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/content-factory/` | Dashboard stats |
| POST | `/admin/content-factory/sources` | Add a content source |
| POST | `/admin/content-factory/crawl` | Trigger collection for a source |
| POST | `/admin/content-factory/process` | Batch AI processing of raw content |
| POST | `/admin/content-factory/review` | Review actions (submit/approve/reject/back_to_draft) |
| POST | `/admin/content-factory/publish` | Publish to internal CMS or social platforms |
| POST | `/admin/content-factory/push-skill` | Push content to an Agent Skill |
| POST | `/admin/content-factory/generate-static` | Generate static HTML pages |
| POST | `/admin/content-factory/push-to-knowledge` | Push content to the knowledge base |
| POST | `/admin/content-factory/cron/tick` | Scheduled collection trigger (X-Cron-Secret) |

### Provided Hooks

| Hook Identifier | Description |
|-------------|------|
| `content_factory/collect` | Trigger content collection |
| `content_factory/process` | Trigger AI content processing |
| `content_factory/publish` | Trigger content publishing |
| `content_factory/push_skill` | Trigger Skill push |

### Listened Hooks

| Hook Identifier | Description |
|-------------|------|
| `cron.tick` | Scheduled task trigger for automatic collection |

## License

This plugin is part of the VeroRun platform and follows the platform's unified license agreement.
