# IM Gateway (im_gateway)

## Overview

IM Gateway is VeroRun's unified instant-messaging channel management plugin. Using an Adapter pattern, it provides centralized configuration management and message push for mainstream IM platforms including Feishu, WeCom, QQ, DingTalk, Telegram, and LINE. The plugin uses a dedicated PostgreSQL schema (`im_gateway`) to store channel configuration data.

The abstract base class `BaseIMAdapter` defines the unified channel adapter contract; each channel implements its own Adapter subclass with connection testing, config field declaration, and message/media push. All channel configs are managed from the admin panel, with secret fields auto-masked.

## Features

- **Multi-platform Management**: Centralized config for Feishu, WeCom, QQ, DingTalk, Telegram, and LINE
- **Adapter Pattern**: Extensible adapter architecture built on the `BaseIMAdapter` base class
- **Connection Testing**: Per-channel connection tests to validate configuration
- **Secret Masking**: Sensitive fields (token, secret, key) auto-masked; smart merge on update
- **Message Push**: `send_message` Hook for text message push
- **Media Push**: `push_media` Hook for media file push (overridable per adapter)
- **Environment Variable Fallback**: Adapters can declare env fallbacks for reference
- **Seed Data**: Auto-creates default channel configs (Feishu, WeCom, etc.) on first run
- **Data Migration**: Idempotently migrates existing channel configs from the main DB
- **Dedicated Database**: PostgreSQL schema `im_gateway` with the `channel_configs` table

## Architecture

```
Admin panel UI
      ↓
Routing layer (routes.py /admin/channels/*)
  GET /            list channels
  GET /<channel>   channel detail
  PUT /<channel>   save/update channel config
  POST /<channel>/test  test connection
      ↓
Adapter layer (adapters/)
  base.py (BaseIMAdapter) + feishu.py / wecom.py / qq.py / dingtalk.py / telegram.py / line.py
      ↓
Data layer (models.py)
  PG Schema: im_gateway → channel_configs (channel PK, config_json, is_enabled, timestamps)
```

## Installation & Enablement

### Installation

The plugin ships with VeroRun's default plugin directory; no separate installation step is required.

### Enablement

1. Enable the IM Gateway plugin on the VeroRun admin "Plugin Management" page
2. Open "System" menu group > "IM Gateway"
3. Configure each channel (Feishu, WeCom, QQ, DingTalk, etc.) and run a connection test

## Configuration

| Config Key | Description |
|--------|------|
| `channel` | Channel identifier (feishu/wecom/qq/dingtalk/telegram/line) |
| `config_json` | Channel-specific configuration (tokens, secrets, webhooks) |
| `is_enabled` | Whether the channel is enabled |

Sensitive fields are masked in the admin UI. Adapters may fall back to environment variables when configured.

## API Endpoints

### Provided Hooks

| Hook Identifier | Description |
|-------------|------|
| `im_gateway/send_message` | Push a text message to a channel |
| `im_gateway/push_media` | Push a media file to a channel |

### Admin Panel

| Path | Description |
|------|------|
| `/admin/channels/` | Channel list and configuration |
| `/admin/channels/<channel>/test` | Test channel connection |

## Dependencies

### Internal Dependencies

- VeroRun core framework: Hook system, route registration
- Admin panel (auth-center): menu rendering

### Dependents

- **health_check** plugin: Feishu/DingTalk alert channels
- **chatbot** plugin: Telegram/LINE channel credentials
- **content_factory** plugin: social media publishing

## License

This plugin is part of the VeroRun project and follows the overall license of the VeroRun project.
