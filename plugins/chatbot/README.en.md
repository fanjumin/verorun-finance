# AI Advisor (chatbot)

## Overview

AI Advisor is VeroRun's site-wide intelligent customer-service plugin, providing LLM-based AI conversation. The plugin uses a dedicated PostgreSQL schema (`chatbot`) for self-contained configuration management, session records, and its Agent registry, while cross-reading the main database for knowledge base data and user ticket information.

The plugin ships with the Advisor Agent (`chat_assistant`), powered by DashScope qwen-turbo, and provides three core capabilities: FAQ Q&A, ticket lookup, and automatic human handoff. It supports multi-channel access (Telegram, LINE), automatic intent recognition, ticket creation, and escalation.

## Features

- **AI Conversation**: Based on the DashScope qwen-turbo LLM, providing natural language understanding and replies
- **Multi-channel Access**: Web floating button, Telegram Bot Webhook, LINE Messaging Webhook
- **FAQ Q&A**: Agent capability `chatbot.faq` — retrieves matching answers from the knowledge base
- **Ticket Lookup**: Agent capability `chatbot.ticket` — cross-reads user ticket status from the main DB
- **Automatic Human Handoff**: Keyword matching and failure-count thresholds, auto-creates tickets (`[TICKET_CREATE]` marker parsing)
- **Conversation Stats**: Today overview, hot-topic ranking, agent performance analytics
- **Conversation QA**: Per-turn quality analysis (QA Check)
- **Agent Copilot**: AI reply suggestions for human agents
- **CSAT**: 1–5 star satisfaction ratings from users
- **Dedicated Database**: PostgreSQL schema `chatbot` with three core tables: `plugin_configs`, `agent_registry`, `chatbot_sessions`
- **Data Migration**: On first start, idempotently migrates config, agent registry entries, and the last 30 days of sessions from the main DB

## Architecture

```
+---------------------------------------------------+
|                    Frontend layer                   |
|  +----------------+  +----------------+  +--------+ |
|  | Web Float Btn  |  | Telegram Bot  |  | LINE Bot| |
|  +-------+--------+  +-------+--------+  +----+---+ |
+----------+-------------------+----------------+------+
           |                   |                |
           v                   v                v
+---------------------------------------------------+
|                Routing layer (routes.py)           |
|  +---------------------------------------------+   |
|  |  chatbot_bp (/admin/chatbot)                 |   |
|  |  +-- /chat               AI conversation     |   |
|  |  +-- /settings           config read/write   |   |
|  |  +-- /stats              stats overview      |   |
|  |  +-- /hot_topics         hot topics          |   |
|  |  +-- /agent_performance  agent performance   |   |
|  |  +-- /qa_check           conversation QA     |   |
|  |  +-- /copilot_suggest    Agent Copilot       |   |
|  |  +-- /csat               satisfaction rating |   |
|  |  +-- /handoff_rules      handoff rules       |   |
|  |  +-- /escalate           create ticket       |   |
|  +---------------------------------------------+   |
|  +---------------------------------------------+   |
|  |  webhook_bp (/api/v1/channels)              |   |
|  |  +-- /telegram/webhook   Telegram Webhook   |   |
|  |  +-- /line/webhook       LINE Webhook       |   |
|  +---------------------------------------------+   |
+----------------------+----------------------------+
                       |
                       v
+---------------------------------------------------+
|            Channel router (channels/router.py)      |
|  +---------------------------------------------+   |
|  |  telegram_handle_webhook()                  |   |
|  |  line_handle_webhook()                      |   |
|  |  _call_ai()              unified AI entry    |   |
|  |  _get_channel_config()   read IM Gateway cfg |   |
|  +---------------------------------------------+   |
+----------------------+----------------------------+
                       |
                       v
+---------------------------------------------------+
|                    Data layer                       |
|  +------------------------+  +-------------------+ |
|  |  chatbot schema (own)  |  |  Main DB (RO)     | |
|  |  +-- plugin_configs     |  |  +-- user_tickets | |
|  |  +-- agent_registry     |  |  +-- knowledge_base| |
|  |  +-- chatbot_sessions   |  |  +-- users        | |
|  +------------------------+  +-------------------+ |
+---------------------------------------------------+
```

**Core design principles**:

- **Dedicated DB + read-only main DB**: The plugin owns a PG schema `chatbot` and never pollutes the main DB; it cross-reads the main DB only for data such as user tickets
- **Unified multi-channel routing**: `channels/router.py` handles Telegram and LINE messages through the same AI engine
- **Local Agent registration**: Maintains an `agent_registry` table in its own schema, avoiding write dependency on the main DB `agent_matrix`

## Directory Layout

```
chatbot/
+-- README.md                    # Plugin documentation
+-- plugin.json                  # Plugin metadata
+-- __init__.py                  # Plugin entry, blueprint registration and init
+-- models.py                    # Data models (DB connection, tables, config, agent registry, migration)
+-- routes.py                    # Admin API routes (chat, config, stats, QA, handoff, etc.)
+-- stats.py                     # Stats module (sessions, today stats, CSAT, hot topics, performance)
+-- channels/
|   +-- __init__.py
|   +-- router.py                # Multi-channel router core (Telegram/LINE webhooks)
+-- prompts/
|   +-- sub_chatbot_prompt.md    # Advisor Agent system prompt
+-- templates/
    +-- admin_chatbot.html       # Admin page template
```

## Installation & Enablement

### Prerequisites

- VeroRun platform version >= 0.10.0
- DashScope API Key (for the qwen-turbo model)
- PostgreSQL database (the plugin uses its own schema `chatbot`)

### Install Steps

1. Place the `chatbot` directory under `plugins/`
2. Make sure `enabled` is `true` in `plugin.json`
3. Restart the application — the plugin auto-initializes:
   - Creates the PostgreSQL schema `chatbot`
   - Creates the three core tables: `plugin_configs`, `agent_registry`, `chatbot_sessions`
   - Idempotently migrates existing config, agent registry entries, and the last 30 days of sessions from the main DB
4. Configure the plugin in Admin > "AI & Content" > "AI Advisor"

### Enabling Multi-channel (optional)

**Telegram**:

1. Configure a Telegram channel (bot_token, etc.) in the IM Gateway plugin
2. Set the environment variable `TELEGRAM_SECRET_TOKEN` (webhook authentication)
3. Point the Telegram Bot webhook to `https://<your-domain>/api/v1/channels/telegram/webhook`

**LINE**:

1. Configure a LINE channel (access_token, etc.) in the IM Gateway plugin
2. Set the environment variable `LINE_CHANNEL_SECRET` (signature verification)
3. Point the LINE Messaging webhook to `https://<your-domain>/api/v1/channels/line/webhook`

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | true | Whether AI Advisor is enabled |
| `auto_escalate` | boolean | true | Auto-create a ticket on handoff |
| `title` | string | "AI Advisor" | Chat window title |
| `subtitle` | string | "Powered by AI Engine" | Chat window subtitle |
| `welcome_message` | string | - | Welcome message |
| `help_hint` | string | - | Help hint text |
| `avatar_url` | string | "" | Bot avatar image URL |
| `agent_id` | string | "chat_assistant" | Bound Agent identifier |
| `max_history` | integer | 20 | Max conversation history rounds (1-50) |
| `float_button_text` | string | "AI Advisor" | Floating button text |
| `handoff_keywords` | JSON array | ["human","agent",...] | Keywords that trigger handoff |
| `handoff_max_fails` | integer | 3 | Consecutive-failure threshold for auto handoff |

## API Endpoints

### Admin APIs (require admin permission)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/chatbot/chat` | Send a message and get the AI reply |
| GET | `/admin/chatbot/settings` | Get all plugin config |
| POST | `/admin/chatbot/settings` | Save plugin config (batch update) |
| POST | `/admin/chatbot/log_session` | Log one conversation turn (requires login) |
| GET | `/admin/chatbot/stats` | Get today's stats overview |
| GET | `/admin/chatbot/hot_topics` | Get today's top-10 hot topics |
| GET | `/admin/chatbot/agent_performance` | Get agent performance data |
| POST | `/admin/chatbot/qa_check` | Conversation QA analysis (requires login) |
| POST | `/admin/chatbot/copilot_suggest` | Agent Copilot reply suggestions (requires login) |
| POST | `/admin/chatbot/csat` | Submit a CSAT rating (1-5, requires login) |
| GET | `/admin/chatbot/handoff_rules` | Get handoff rule config |
| POST | `/admin/chatbot/handoff_rules` | Save handoff rule config |
| POST | `/admin/chatbot/escalate` | AI handoff, creates a ticket (requires login) |

### Public Webhook Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/channels/telegram/webhook` | X-Telegram-Bot-Api-Secret-Token header | Telegram Bot webhook receiver |
| POST | `/api/v1/channels/line/webhook` | x-line-signature header (HMAC-SHA256) | LINE Messaging webhook receiver |

## Dependencies

### Internal Dependencies

| Dependency | Purpose |
|------------|---------|
| `plugins._base.db` | Base plugin DB module, provides `get_raw_connection()` |
| `auth-center.models` | Main DB reads (user_tickets, knowledge_base, users, etc.) |
| `auth-center.services.jwt_service` | JWT token validation (`validate_token`) |
| `agent_matrix.engine` | Unified LLM engine (`UnifiedLLM`) |
| `agent_matrix.intent` | Intent classification (`classify_intent`) |
| `plugins.im_gateway` | IM Gateway plugin, reads Telegram/LINE channel credentials |

### External Dependencies

| Dependency | Purpose |
|------------|---------|
| DashScope API (qwen-turbo) | AI conversation model |
| Telegram Bot API | Multi-channel message sending (`sendMessage`) |
| LINE Messaging API | Multi-channel replies (`replyMessage`) |

### Provided Hooks

| Hook ID | Description |
|---------|-------------|
| `chatbot/config` | Get chatbot config |
| `chatbot/chat` | Run an AI conversation |

### Agent Registration

| Attribute | Value |
|-----------|-------|
| Name | Advisor Agent |
| Identifier | `chat_assistant` |
| Role type | sub |
| Domain | chatbot |
| Model policy | tier: standard, user override allowed |
| Capabilities | `chatbot.faq`, `chatbot.ticket`, `chatbot.human_handoff` |

## License

This plugin is part of the VeroRun platform and follows the platform's unified license agreement.
