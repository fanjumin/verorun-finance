# Health Check (health_check)

## Overview

Health Check is VeroRun's automated health monitoring plugin, providing an extensible system health check framework with scheduled inspection, dashboard visualization, multi-channel alerting, Workflow engine integration, and AI-assisted fixing. It targets production system operation and monitoring scenarios.

## Features

- **Extensible Checker Framework**: Plugin-based design supports custom health checkers for flexible extension
- **Scheduled Inspection**: Built-in scheduler runs health checks on cron expressions
- **Dashboard Visualization**: Embeds system health score and historical trends in the admin panel
- **Multi-channel Alerting**: Email, site message, Webhook, Feishu, and DingTalk alert channels
- **Workflow Integration**: Health check results can be used as Workflow trigger conditions
- **AI-assisted Fixing**: Built-in AI fixer suggests or executes fixes based on check results
- **Service Discovery**: Automatically discovers monitored services and components
- **Health Score**: Aggregates multiple check metrics into an overall system health score

## Architecture

The plugin stores data in the PostgreSQL `health` schema (8 tables) and uses SQLite `data/health.db` for local development.

```
scheduler_setup.py (scheduler init)
   ├── discovery.py   (service discovery)
   ├── checkers.py    (checker framework)
   └── metrics.py     (metric collection)
            ↓
        models.py     (8 table ORM)
            ↓
   alerter.py / ai_fixer.py / routes.py
```

| Table | Purpose |
|------|------|
| `health_checks` | Health check item definitions |
| `health_check_results` | Check result records |
| `health_alerts` | Alert records |
| `health_alert_rules` | Alert rule configuration |
| `health_alert_channels` | Alert channel configuration |
| `health_schedules` | Inspection schedules |
| `health_metrics` | Health metric time series |
| `health_components` | Monitored component registry |

## Installation & Enablement

### Installation

The plugin ships with VeroRun's default plugin directory; no separate installation step is required.

### Enablement

1. Ensure the `health` schema exists in PostgreSQL
2. Enable the Health Check plugin on the VeroRun admin "Plugin Management" page
3. After enabling, the scheduler starts automatically and runs inspections per the configured cron
4. The "Monitoring & Data" menu group will show the Health Check entry

## Configuration

| Config Key | Description | Default |
|--------|------|--------|
| `database.schema` | PostgreSQL schema name | `health` |
| `scheduler.enabled` | Enable scheduled inspection | `true` |
| `scheduler.cron` | Inspection cron expression | `*/5 * * * *` (every 5 min) |
| `alert_channels.email.enabled` | Enable email alerts | `true` |
| `alert_channels.site_message.enabled` | Enable site message alerts | `true` |
| `alert_channels.webhook.enabled` | Enable Webhook alerts | `false` |
| `alert_channels.feishu.enabled` | Enable Feishu alerts | `false` |
| `alert_channels.dingtalk.enabled` | Enable DingTalk alerts | `false` |
| `ai_fixer.enabled` | Enable AI fixer | `true` |
| `ai_fixer.auto_fix` | Auto-execute fixes | `false` |

## API Endpoints

### Provided Hooks

| Hook Identifier | Type | Description |
|-------------|------|------|
| `health/run_check` | Hook | Manually trigger a health check |
| `health/get_status` | Hook | Get current system health status |
| `health/get_trend` | Hook | Get health trend data |

### Admin Panel

| Path | Description |
|------|------|
| `/admin/health/` | Health check dashboard (embedded page) |

### Filter Registered

| Filter Identifier | Description |
|---------------|------|
| `dashboard.data` | Injects health score summaries into the admin dashboard |

## Dependencies

### Internal Dependencies

- VeroRun core framework: Hook system, event bus, scheduler
- Admin panel (auth-center): dashboard embedding and menu rendering
- **email** plugin: email alert channel
- **im_gateway** plugin: Feishu/DingTalk alert channels

### External Dependencies

- **PostgreSQL**: production data storage (`health` schema)

### Dependents

- **Workflow engine**: calls health check results as workflow trigger conditions
- **analytics** plugin: reads analytics data as reference metrics

## License

This plugin is part of the VeroRun project and follows the overall license of the VeroRun project.
