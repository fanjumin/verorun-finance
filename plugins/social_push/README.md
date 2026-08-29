# Social Push (social_push)

## Overview

Social Push is VeroRun's multi-platform social media content publishing plugin. It supports seven platforms: WeChat Official Account, Weibo, Toutiao, Twitter/X, LinkedIn, Reddit, and Telegram Channel. The plugin stores publish history in a dedicated PostgreSQL schema `social_push`, while delegating the core publishing logic to the corresponding service layer in auth-center.

The plugin uses a Provider pattern to manage international platforms (Twitter, LinkedIn, Reddit, Telegram); domestic platforms (WeChat, Weibo, Toutiao) directly reuse auth-center's push services. AI copywriting and AI image generation go through the platform-wide shared LLM service (`ai_content_generator`) and are decoupled from the publishing channels.

## Features

- **Seven platforms**: WeChat Official Account, Weibo, Toutiao, Twitter/X, LinkedIn, Reddit, Telegram Channel
- **Provider pattern**: international platforms extend via adapters under `providers/` with a unified interface
- **AI copywriting**: Tongyi Qianwen based content generation (shared LLM service, not plugin-private)
- **AI cover image**: Tongyi Wanxiang based cover image generation (shared LLM service)
- **Publish history**: complete publish log with per-platform filtering and pagination
- **CMS article import**: import already-published CMS articles into the social editor
- **Publish status query**: WeChat publish status callback support
- **Config detection**: automatically detects each platform's config status, distinguishing configured vs unconfigured platforms
- **Market awareness**: distinguishes domestic/international markets via the `DEPLOY_MARKET` env var, dynamically showing available platforms
- **Dedicated database**: PostgreSQL schema `social_push` with the `social_push_logs` table
- **Data migration**: idempotently migrates historical publish records from the main database on first startup

## Architecture

```
+--------------------------------------------------------------+
|                    Admin Dashboard UI                         |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                     Route layer (routes.py)                   |
|  /admin/social/*                                             |
|  +-- /check-config     Platform config detection             |
|  +-- /content-types    Content type query                    |
|  +-- /generate         AI copywriting generation             |
|  +-- /generate-image   AI image generation                   |
|  +-- /publish          Multi-platform publishing             |
|  +-- /publish-status   WeChat publish status query           |
|  +-- /history          Publish history query                 |
|  +-- /import-from-cms  CMS article import                    |
|  +-- /history/<id>     Delete publish record                 |
+--------------------------------------------------------------+
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
+------------------+ +------------------+ +------------------+
| _publish_wechat  | | _publish_weibo   | | _publish_toutiao |
| (WeChat draft +  | | (Weibo publish)  | | (Toutiao publish)|
|  publish)        | |                  | |                  |
+------------------+ +------------------+ +------------------+
                              |
                              v
              +-------------------------------+
              | _publish_via_provider()       |
              | (Twitter/LinkedIn/Reddit/     |
              |  Telegram unified entry)      |
              +-------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                   Provider layer (providers/)                 |
|  +-- base.py               Provider abstract base            |
|  +-- twitter.py            Twitter/X Provider                |
|  +-- linkedin.py           LinkedIn Provider                 |
|  +-- reddit.py             Reddit Provider                   |
|  +-- telegram_channel.py   Telegram Channel Provider         |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                       Data layer (models.py)                  |
|  PG Schema: social_push                                       |
|  +-- social_push_logs    Publish history table               |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                   auth-center service layer                   |
|  +-- services/wechat_push_service.py   WeChat push service    |
|  +-- services/weibo_service.py         Weibo push service     |
|  +-- services/toutiao_service.py       Toutiao push service   |
|  +-- services/ai_content_generator.py  Shared AI service      |
+--------------------------------------------------------------+
```

**AI capabilities are decoupled from publishing channels**:

```
Creation tools (ai_capabilities)          Channels (platforms)
+---------------------------+      +---------------------------+
| AI copywriting (Qianwen)  |      | WeChat OA (wechat)        |
| AI cover image (Wanxiang) |      | Weibo (weibo)             |
| Via shared LLM service    |      | Toutiao (toutiao)         |
| (ai_content_generator)    |      | Twitter/X (twitter)       |
+---------------------------+      | LinkedIn (linkedin)       |
                                   | Reddit (reddit)           |
                                   | Telegram (telegram)       |
                                   +---------------------------+
```

## Directory Structure

```
social_push/
+-- README.md                    # Plugin documentation (English)
+-- README_CN.md                 # Plugin documentation (Chinese)
+-- plugin.json                  # Plugin metadata configuration
+-- __init__.py                  # Plugin entry, blueprint & Hook registration
+-- models.py                    # Data model (connection, table creation, main-db migration)
+-- routes.py                    # Admin API routes (config detection, AI generation, publish, history)
+-- providers/
|   +-- __init__.py              # Provider registration & factory functions
|   +-- base.py                  # Provider abstract base class
|   +-- twitter.py               # Twitter/X Provider
|   +-- linkedin.py              # LinkedIn Provider
|   +-- reddit.py                # Reddit Provider
|   +-- telegram_channel.py      # Telegram Channel Provider
+-- i18n/
|   +-- en.yml                   # English internationalization
|   +-- zh-CN.yml                # Chinese internationalization
+-- templates/
    +-- admin_socialpush.html    # Admin dashboard page template
```

## Install & Enable

### Prerequisites

- VeroRun platform version >= 0.10.0
- API credentials for the target platforms (e.g. WeChat Official Account AppID/AppSecret, Weibo App Key, etc.)
- DashScope API Key (for AI copywriting and cover image generation)
- PostgreSQL database

### Install Steps

1. Place the `social_push` directory under `plugins/`
2. Restart the application; the plugin auto-registers on startup and:
   - Creates the PostgreSQL schema `social_push`
   - Initializes the `social_push_logs` table
   - Idempotently migrates historical publish records from the main database
3. Configure each platform in the admin dashboard under "Publishing" > "Social Push"
4. Use the `check-config` endpoint to verify the config status of each platform

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DEPLOY_MARKET` | Deployment market: `cn` (domestic, only shows domestic platforms) / `intl` (international, shows all platforms) |

## Configuration

Platform configurations are managed independently and stored in auth-center's `system_config` table.

### Domestic Platforms

| Platform | Config keys |
|----------|-------------|
| WeChat Official Account | `wechat_app_id`, `wechat_app_secret` |
| Weibo | `weibo_app_key`, `weibo_access_token` |
| Toutiao | `toutiao_app_id`, `toutiao_access_token` |

### International Platforms

| Platform | Config keys |
|----------|-------------|
| Twitter/X | `twitter_api_key`, `twitter_api_secret`, `twitter_access_token`, `twitter_access_secret`, `twitter_bearer_token` |
| LinkedIn | `linkedin_client_id`, `linkedin_client_secret`, `linkedin_access_token` |
| Reddit | `reddit_client_id`, `reddit_client_secret`, `reddit_username`, `reddit_password` |
| Telegram | `telegram_bot_token`, `telegram_channel` |

### AI Capabilities

| Capability | Config key |
|------------|------------|
| AI copywriting (Tongyi Qianwen) | `dashscope_text_key` |
| AI cover image (Tongyi Wanxiang) | `dashscope_api_key` |

## API Endpoints

### Config Detection

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/social/check-config` | Detects config status of each platform and AI capability |

### AI Content Generation

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/social/generate` | AI copywriting generation (supports topic, content_type, temperature) |
| POST | `/admin/social/generate-image` | AI image generation (supports prompt, title, cover mode) |

### Content Types

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/social/content-types` | Queries the content types supported by each platform |

### Publishing

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/social/publish` | Publishes content to the given platforms (multi-platform in one request) |
| GET | `/admin/social/publish-status/<publish_id>` | Queries WeChat publish status |

### Publish History

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/social/history` | Paginated publish history (supports platform filtering) |
| DELETE | `/admin/social/history/<id>` | Deletes a publish history record |

### CMS Import

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/social/import-from-cms` | Lists published CMS articles for import |

### Publish Request Body Example

```json
{
  "title": "Article title",
  "body": "Article body",
  "body_html": "<h1>HTML body</h1>",
  "summary": "Summary",
  "author": "admin",
  "cover_image_url": "/static/uploads/cover.jpg",
  "platforms": ["wechat", "weibo", "twitter"],
  "auto_publish": false
}
```

## Platform Content Types

| Platform | Supported content types |
|----------|-------------------------|
| WeChat Official Account | article, announcement, promotion |
| Weibo | weibo |
| Toutiao | article |
| Twitter/X | tweet |
| LinkedIn | article, post |
| Reddit | link, text |
| Telegram | message |

## Dependencies

### Internal Dependencies

| Dependency | Purpose |
|------------|---------|
| `plugins._base.db` | Plugin base database connection module |
| `auth-center.models` | Main-db reads (system_config, cms_posts) |
| `auth-center.services.wechat_push_service` | WeChat push (`create_draft`, `submit_publish`, `upload_article_image`) |
| `auth-center.services.weibo_service` | Weibo publish (`publish_weibo`) |
| `auth-center.services.toutiao_service` | Toutiao publish (`publish_article`) |
| `auth-center.services.ai_content_generator` | Shared AI service (`generate_article`, `generate_image`, `generate_cover_image`) |

### External Dependencies

| Dependency | Purpose |
|------------|---------|
| WeChat Official Account API | Draft creation & publishing |
| Weibo Open Platform API | Weibo publishing |
| Toutiao API | Toutiao article publishing |
| Twitter/X API | Tweet publishing |
| LinkedIn API | Article/update publishing |
| Reddit API | Post publishing |
| Telegram Bot API | Channel message publishing |
| DashScope (Tongyi Qianwen) | AI copywriting generation |
| DashScope (Tongyi Wanxiang) | AI cover image generation |

### Provided Hooks

| Hook identifier | Description |
|-----------------|-------------|
| `social_push/publish` | Publishes content to social media platforms |

## Menu Group

- **Publishing** - Social Push

## Extension Guide

### Adding a New Social Media Platform

1. Create a new Provider file under `providers/` (e.g. `facebook.py`)
2. Inherit `providers.base.BaseSocialProvider` and implement the `publish()` and `get_config_fields()` methods
3. Register the new platform in the `get_provider()` factory in `providers/__init__.py`
4. Add a route branch in `_publish_to_platform()` in `routes.py`

```python
# providers/facebook.py example
from .base import BaseSocialProvider

class FacebookProvider(BaseSocialProvider):
    platform_id = 'facebook'
    platform_name = 'Facebook'

    def get_config_fields(self):
        return [
            {'key': 'facebook_app_id', 'label': 'App ID', 'type': 'text'},
            {'key': 'facebook_app_secret', 'label': 'App Secret', 'type': 'password'},
            {'key': 'facebook_page_token', 'label': 'Page Access Token', 'type': 'password'},
        ]

    def publish(self, title, body, summary, image_url, link_url, config):
        # Implement the Facebook Graph API publishing logic
        ...
```

## License

This plugin is part of the VeroRun platform and follows the platform's unified license agreement.

## Change Log

### v1.2.0 (2026-08-06)

Audit-driven fixes (19 findings fully resolved):

**Blocking**
- Reddit publishing now works end-to-end: the subreddit is passed through from the publish form to the Reddit provider
- `_get_international_providers()` signature aligned with its call site so provider configs are passed, making `is_configured()` accurate
- Version unified to 1.2.0 across `plugin.json` / `__init__.py`

**High**
- LinkedIn `shareMediaCategory` now switches between `ARTICLE` / `NONE` based on whether a link is provided
- tweepy response accessed via `resp.data.id` instead of `resp.data['id']`
- Removed the external `get_market` import — the market is read directly from the `DEPLOY_MARKET` env var
- `category` normalized to the v1.4 standard enumeration (`social`)

**Medium**
- `settings_schema` fully declared with valid JSON Schema (20 config items)
- Thread-safe per-thread DB connections (`threading.local`)
- `created_at` migrated to `TIMESTAMPTZ` with an idempotent ALTER for legacy records
- `content_factory` availability check on the admin page (AI buttons disabled when unavailable)
- Missing "Import from CMS" i18n keys added (en / zh-CN)
- v1.4 store compliance: `dashboard.stats` declared + `get_dashboard_stats()` implemented

**Low**
- Toutiao success message quote typo fixed
- `sys.path` pollution removed from the plugin entry
- Non-standard `enabled` field removed from `plugin.json`
- Telegram sends photos via `sendPhoto` when an image URL is provided
- Minor i18n string spacing fixes
