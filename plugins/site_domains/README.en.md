# Site Domains (site_domains)

## Overview

Site Domains is VeroRun's subdomain management and web server configuration generation plugin. The plugin manages multi-site domain binding relationships and can automatically generate server configuration files for both Nginx and Caddy. It also registers a Caddy On-Demand TLS certificate validation endpoint to enable automated HTTPS certificate management.

## Features

- **Subdomain Management**: Manage binding relationships between multiple sites and domains (CRUD provided through the auth-center admin_bp)
- **Nginx Configuration Generation**: Automatically generate Nginx virtual host configurations based on domain bindings
- **Caddy Configuration Generation**: Automatically generate Caddyfile configurations based on domain bindings
- **Caddy On-Demand TLS Validation**: Registers a certificate validation endpoint to enable automated HTTPS with Caddy
- **Lightweight Design**: Contains a single routing module; CRUD is provided centrally by auth-center

## Architecture

### Database Strategy

The plugin uses **no separate database**; it reads directly from the `site_domains` table in the VeroRun main database.

### Table Structure

#### site_domains (main database table)

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `site_id` | Integer | Associated site ID |
| `domain` | String(255) | Domain name |
| `is_primary` | Boolean | Whether it is the primary domain |
| `ssl_enabled` | Boolean | Whether SSL is enabled |
| `status` | String(50) | Domain status (active / pending / error) |
| `created_at` | DateTime | Creation time |
| `updated_at` | DateTime | Update time |

### Module Structure

```
┌─────────────────────────────────────────────────────────────────┐
│                       routes.py                                  │
│          (Caddy On-Demand TLS validation / config generation)     │
└─────────────────────────────┬───────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌──────────────────┐ ┌────────────────┐ ┌──────────────────┐
│ Caddy On-Demand  │ │ Nginx config   │ │ auth-center      │
│ TLS validation   │ │ generation     │ │ admin_bp (CRUD)  │
└──────────────────┘ └────────────────┘ └──────────────────┘
```

### Data Flow

1. **Domain Management**: Administrators perform CRUD operations on domains through the auth-center admin_bp; data is written to the `site_domains` table in the main database
2. **Configuration Generation**: The plugin's `routes.py` provides configuration generation endpoints, reading domain bindings from the main database and generating Nginx/Caddy configurations
3. **TLS Validation**: When Caddy processes an On-Demand TLS certificate request, it calls back to the validation endpoint registered by this plugin to verify domain legitimacy

## Directory Structure

```
site_domains/
├── __init__.py                  # Plugin entry, registers routes and menu
├── routes.py                    # Caddy validation endpoint + config generation endpoints
├── plugin.json                  # Plugin metadata configuration
└── templates/
    └── admin_sitedomains.html   # Admin backend domain management page template
```

## Installation & Enablement

### Installation

The plugin is included in VeroRun's default plugin directory; no extra installation steps are needed.

### Enablement

1. Enable the Site Domains plugin on the VeroRun admin "Plugin Management" page
2. A domain management entry will appear under the "System" menu group in the admin backend
3. To use Caddy On-Demand TLS, configure the validation endpoint in the Caddyfile:

```caddyfile
*.your-domain.com {
    tls {
        on_demand
    }
    reverse_proxy localhost:5000
}
```

## Configuration

Configure the following parameters in `plugin.json`:

```json
{
  "name": "site_domains",
  "version": "0.1.0",
  "database": {
    "use_main_db": true,
    "table": "site_domains"
  },
  "caddy": {
    "on_demand_tls_endpoint": "/api/site_domains/caddy/verify",
    "allowed_domains_pattern": "*.your-domain.com"
  },
  "nginx": {
    "config_output_path": "/etc/nginx/sites-enabled/",
    "template": "default"
  }
}
```

| Config Key | Description | Default |
|------------|-------------|---------|
| `database.use_main_db` | Whether to use the main database | `true` |
| `database.table` | Domain table name in the main database | `site_domains` |
| `caddy.on_demand_tls_endpoint` | Caddy TLS validation endpoint path | `/api/site_domains/caddy/verify` |
| `caddy.allowed_domains_pattern` | Allowed domain pattern | none |
| `nginx.config_output_path` | Nginx configuration output directory | `/etc/nginx/sites-enabled/` |

## API Endpoints

### Caddy On-Demand TLS Validation

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/site_domains/caddy/verify?domain=<domain>` | Domain validation before Caddy certificate issuance |

### Configuration Generation

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/site_domains/generate/nginx` | Generate Nginx configuration |
| `POST` | `/api/site_domains/generate/caddy` | Generate Caddy configuration |

### Admin Backend

| Menu Item | Group | Description |
|-----------|-------|-------------|
| `Site Domains` | `System` | Domain management page |

## Dependencies

### Internal Dependencies

- VeroRun core framework: route registration, main database ORM
- Admin backend (auth-center): provides the admin_bp CRUD capability

### External Dependencies

- **Caddy**: Web server (On-Demand TLS requires Caddy)
- **Nginx**: Web server (config generation requires Nginx)

### Dependents

- Site system: multi-site domain binding capability
- Caddy server: On-Demand TLS certificate validation

## License

This plugin is part of the VeroRun project and follows the VeroRun project's overall license agreement.
