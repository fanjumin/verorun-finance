# Captcha Service (captcha_embedded)

## Overview

Captcha Service is VeroRun's slider captcha plugin, providing puzzle generation, behavior analysis, and rate limiting. The plugin follows a self-contained architecture: core logic (generator, security, behavior analysis, storage) lives inside the plugin's own `captcha/` package, while REST routes are exposed by its own `routes.py` (url_prefix `/api/captcha`).

## Features

- **Slider Captcha Generation**: Dynamically generates puzzle-style slider captchas with background and slider images
- **Behavior Analysis**: Analyzes drag behavior (trajectory, speed, timing) to determine whether the interaction is from a real human
- **Rate Limiting**: Built-in rate limiting to prevent captcha API abuse
- **Cookie-less Verification**: Verification does not depend on client cookies; state is managed through server-side tokens
- **Lightweight Integration**: Exposes captcha capabilities through Hook interfaces as an embedded plugin

## Architecture

### Storage Strategy

The plugin uses **Redis with in-memory fallback** to store temporary captcha state (tokens, rate limits, bans, stats). Redis provides persistence (RDB/AOF), which suits short-lived (TTL <= 300s) captcha data and keeps hot-path latency (generate/verify/consume) low.

Per plugin standard v1.4 (section 9.1 / 11.2), `models.py` declares this strategy and reserves a PostgreSQL schema `captcha_embedded` (table definitions + `init_captcha_db()`). A future full migration to PG can call the initializer and switch the `store.py` read/write layer.

### Verification Flow

1. **Generate**: The client requests the `captcha/generate` Hook; the server creates the puzzle image and a verification token
2. **Present**: The frontend renders the slider captcha; the user drags the slider to complete the puzzle
3. **Verify**: The frontend submits drag behavior data; the server runs behavior analysis via the `captcha/verify` Hook
4. **Consume**: The business layer calls the `captcha/consume` Hook to consume the token, ensuring one-time use

## Directory Structure

```
captcha_embedded/
├── __init__.py          # Plugin entry: lifecycle + route registration + Dashboard stats
├── plugin.json          # Plugin metadata (plugin-standard v1.4 compliant)
├── routes.py            # Blueprint (url_prefix=/api/captcha)
├── config.py            # Configuration (lazy SECRET_KEY validation)
├── models.py            # Storage strategy + reserved PG schema (init_captcha_db)
├── images/              # Puzzle background images (self-contained, 26 images)
├── captcha/             # Core logic
│   ├── __init__.py
│   ├── generator.py     # Puzzle generation
│   ├── security.py      # HMAC token generation/validation
│   ├── behavior.py      # Behavior trajectory analysis and risk scoring
│   └── store.py         # Storage: Redis + in-memory fallback
├── templates/
│   └── captcha_stats.html  # Stats page bare JS partial
└── i18n/
    ├── en.yml           # English internationalization
    └── zh-CN.yml        # Simplified Chinese internationalization
```

## Installation & Enablement

### Installation

The plugin ships with VeroRun's default plugin directory; no separate installation step is required.

### Enablement

1. Enable the Captcha Service plugin on the VeroRun admin "Plugin Management" page
2. After enabling, the `captcha/generate`, `captcha/verify`, and `captcha/consume` Hooks register automatically
3. Frontend pages can call the captcha Hooks for human verification

## Configuration

The following parameters are configured in `plugin.json`:

| Config Key | Description | Default |
|--------|------|--------|
| `captcha.puzzle_size.width` | Puzzle block width (px) | `60` |
| `captcha.puzzle_size.height` | Puzzle block height (px) | `60` |
| `captcha.background_size.width` | Background image width (px) | `320` |
| `captcha.background_size.height` | Background image height (px) | `160` |
| `captcha.tolerance` | Slider position tolerance (px) | `5` |
| `captcha.expire_seconds` | Verification token expiry (seconds) | `300` |
| `captcha.rate_limit.max_requests_per_minute` | Max requests per minute | `10` |
| `captcha.rate_limit.max_requests_per_ip_per_minute` | Max requests per IP per minute | `5` |

## API Endpoints

### Provided Hooks

| Hook Identifier | Type | Description |
|-------------|------|------|
| `captcha/generate` | Hook | Generate a captcha puzzle; returns image data and a verification token |
| `captcha/verify` | Hook | Validate drag behavior; returns the verification result |
| `captcha/consume` | Hook | Consume the verification token, marking the captcha as used |

### Admin Panel

The plugin has no standalone admin menu. `templates/captcha_stats.html` is a stats page bare JS partial, with data served by `GET /api/captcha/admin/stats/`.

## Dependencies

### Internal Dependencies

- VeroRun core framework: Hook system, PluginManager route registration (register_routes)
- Puzzle background images: plugin's own `images/` directory (self-contained)

### External Dependencies

- Third-party Python packages: Pillow (puzzle generation), numpy (pixel processing), redis (storage, optional)

### Consumers

- Any business module requiring human verification can call this plugin's captcha capability through Hooks
- Typical scenarios: login forms, registration forms, sensitive operation confirmation

## License

This plugin is part of the VeroRun project and follows the overall license of the VeroRun project.
