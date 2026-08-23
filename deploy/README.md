# VeroRun Deployment Guide

> Automated one-command deployment script for VeroRun multi-service system on Ubuntu 22.04+.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Install (One Command)](#quick-install-one-command)
- [What the Script Does](#what-the-script-does)
- [Available Commands](#available-commands)
- [Architecture Overview](#architecture-overview)
- [Post-Install Configuration](#post-install-configuration)
- [Troubleshooting](#troubleshooting)
- [Manual Step-by-Step Installation](#manual-step-by-step-installation)
- [Educational Edition（教育版）](#educational-edition教育版)
- [Release Signing（发布签名）](#release-signing发布签名)

---

## Prerequisites

- **OS:** Ubuntu 22.04 or 24.04 LTS (clean installation recommended)
- **User:** `root` access via `sudo` (the script must run as root)
- **Network:** Outbound internet access to GitHub (for cloning the repository)
- **Domain (recommended):** A domain name pointed to your server's public IP
- **Minimum specs:**
  - 2 GB RAM (4 GB recommended)
  - 20 GB disk
  - 1 vCPU (2 vCPU recommended)

---

## Selecting a Distribution

VeroRun is distributed through two repositories — pick the one that matches your deployment:

| Distribution | Repository | When to use |
|--------------|-----------|-------------|
| `verorun-pro` | `https://github.com/fanjumin/verorun-pro` (public) | Standard enterprise package, open download. Install by cloning the repo (see below). |
| `verorun-code` | `https://github.com/fanjumin/verorun-code` (private) | Official site / enterprise customization. Requires SSH access to the private repository. |

`verorun-pro` is generated automatically from `verorun-code` on every version tag by the `sync-to-pro` CI workflow. `install.sh` sets the `GIT_REPO` variable per distribution (HTTPS for `verorun-pro`, SSH for `verorun-code`), so `update` always pulls from the correct source.

### Official Edition（官方版）

The Official Edition is a **private, licensed deployment** of `verorun-code` reserved for
official sites / enterprise customization. It MUST be deployed only with `install-official.sh`,
which lives in `deploy/` of `verorun-code` and is NOT exported to `verorun-pro`.

```bash
git clone git@github.com:fanjumin/verorun-code.git /tmp/verorun-official
cd /tmp/verorun-official
sudo bash deploy/install-official.sh install your-domain.com
```

> ⚠️ **NEVER run `install.sh` on an official server** — it pulls the public `verorun-pro`.
> After a correct official install:
> - `.env` contains `VR_EDITION=official` — `install.sh` refuses to run on such a server
> - `git remote -v` points to `verorun-code` (SSH)
> - `deploy/` no longer contains `install.sh` / `install-code.sh` / `uninstall.sh`
> - `git log` shows a `verorun-code` dev commit, **not** a `Sync from verorun-code` commit
>   (a `Sync from...` commit means the code actually came from `verorun-pro` via CI sync)

## Quick Install (One Command)

Fresh install in a single command — no `git` required (the script auto-fetches the shared
`deploy/lib/common.sh` library from verorun-pro when run via pipe):

### Unified Installer (build-2026.08.11+)

A single `install.sh` replaces the previous four separate scripts. Choose your deployment
type interactively or via the `INSTALL_TYPE` environment variable:

```bash
# Interactive (recommended) — select type from menu
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh | sudo bash -s -- install

# CI / automation — specify type via environment variable
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh | sudo env INSTALL_TYPE=professional bash

# With domain (Website type)
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh | sudo env INSTALL_TYPE=website bash -s -- install your-domain.com
```

**Supported INSTALL_TYPE values:**

| Value | DEPLOY_TYPE | Description | Old Script |
|-------|-------------|-------------|------------|
| `website` | production | Domain + HTTPS, public deployment | `install.sh` |
| `professional` | lan | No domain, LAN access, verorun-pro | `install-local.sh` |
| `development` | dev | No plugins, verorun-code (SSH), requires deploy key | `install-dev.sh` |
| `educational` | edu | No domain, edu license required | (new) |

> `install-code.sh` is preserved as an independent shortcut for Team (code) deployments (full plugins).

### Educational Edition（教育版）

```bash
# One-command edu install (no domain, edu license code required)
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/install.sh | sudo env INSTALL_TYPE=educational bash
```

> **common.sh 同步依赖（audit F-02）：** 教育版一键安装从 `verorun-edu` 拉取
> `deploy/lib/common.sh`，其校验哈希 `EDU_COMMON_SHA256` 与 `verorun-pro` 的
> `COMMON_SHA256` **默认 pin 同一值**。两仓库的 common.sh 内容由
> `sync-to-pro.yml` / `sync-to-edu.yml`（`git archive` 按 tag 导出）自动保持同步；
> 若不同步，教育版一键安装会因哈希不匹配而**校验失败中止**。
>
> 如需独立 pin（例如 edu 仓库 fork 出不同内容），用环境变量覆盖：
>
> ```bash
> # 实际哈希可用 sha256sum deploy/lib/common.sh 计算（LF 归一化）
> curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/install.sh \
>   | sudo env INSTALL_TYPE=educational EDU_COMMON_SHA256=<64-hex> bash
> ```
>
> **许可端点（audit F5）：** 校验前脚本会对许可端点做一次可达性预检。默认端点按
> `REGION` 选择：`cn` → `https://api.verorun.cn`，其余 → `https://api.verorun.com`。
> 离网/内网部署可用 `EDU_LICENSE_ENDPOINT` 环境变量覆盖为内网 license 服务地址；
> 端点不可达时会先打印 `[WARN]` 提示（而非运行期静默失败）。

**Alternatively — clone then run locally:**

### With a domain

```bash
git clone https://github.com/fanjumin/verorun-pro.git
cd verorun-pro
sudo bash deploy/install.sh install your-domain.com
```

Replace `your-domain.com` with your actual domain name.

### Without a domain (configure later)

```bash
git clone https://github.com/fanjumin/verorun-pro.git
cd verorun-pro
sudo bash deploy/install.sh install
```

You will be prompted to enter a domain or skip. If skipped, you can configure it later:

```bash
sudo bash deploy/install.sh configure-domain your-domain.com
```

### `verorun-code` (private repo)

> Use the Development type in `install.sh` for `verorun-code` deployments — it defaults `GIT_REPO`
> to the SSH private repository. Or use `install-code.sh` as a standalone shortcut.
> Do NOT use Website or Professional type for private repos — they pull the public
> `verorun-pro` repository (HTTPS).

```bash
# Via unified installer
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh | sudo env INSTALL_TYPE=development bash

# Or clone then run
git clone git@github.com:fanjumin/verorun-code.git
cd verorun-code
sudo bash deploy/install.sh install     # select [3] Development in interactive menu
```

---

## What the Script Does

On a fresh install (`install` mode), the script:

1. **System dependencies** — Installs Python 3, Nginx, Git, build tools, PostgreSQL
2. **PostgreSQL** — Installs and starts PostgreSQL, creates the `verorun` database role and database
3. **User & directories** — Creates the `verorun` system user, workspace directory, and log directory
4. **Pull code** — Clones the latest code from GitHub into `/home/verorun/verorun/` (via SSH deploy key for `verorun-code`, HTTPS for `verorun-pro`)
5. **Python virtual environment** — Creates a venv and installs all Python dependencies
6. **Environment file** — Generates `.env` with auto-generated secrets (JWT, encryption keys, etc.)
7. **systemd services** — Writes 5 service files:
   - `verorun-main` (port 8081) — Main site backend / auth center (`auth_server`)
   - `verorun-auth` (port 8083) — Platform user console & subscription (`main_site`)
   - `verorun-admin` (port 8084) — Admin panel (`admin`)
   - `verorun-health` (port 8085) — Health service (`health_service`)
   - `verorun-guardian` — VeroGuard unified guardian daemon
8. **Nginx** — Configures reverse proxy for main domain + subdomains
9. **Start services** — Starts all systemd services and Nginx
10. **Database migration + seed** — In `install` mode the script auto-runs `init_db` migration and seeds initial data (admin account, subscription plans, products), so the deployment is fully usable right after install

If no domain is provided, steps 7-9 are skipped and can be run later with `configure-domain`. Steps 1-6 and 10 always run.

---

## Available Commands

| Command | Usage | Description |
|---------|-------|-------------|
| `install` | `install.sh install [domain]` | Fresh installation (default if no `.env` exists) |
| `update` | `install.sh update` | Pull latest code, update deps, restart services |
| `restart` | `install.sh restart` | Restart all systemd services and Nginx |
| `health` | `install.sh health` | Check all services and show HTTP status |
| `rollback` | `install.sh rollback` | Revert to previous git commit and restart |
| `seed` | `install.sh seed` | Inject initial data (admin account, plans, products) |
| `configure-domain` | `install.sh configure-domain <domain>` | Set/replace domain, re-configure Nginx and services |

### Example: Update to latest code

```bash
sudo bash deploy/install.sh update
```

### Example: Health check

```bash
sudo bash deploy/install.sh health
```

### Example: Seed initial data (after install)

```bash
sudo bash deploy/install.sh seed
```

---

## Clean Uninstall

Remove everything (services, databases, code, logs) for a complete fresh start —
use the dedicated uninstall script (idempotent and re-runnable):

```bash
sudo env VR_UNINSTALL_YES=1 bash deploy/uninstall.sh
```

`uninstall.sh` performs, in order:

1. Stop & disable all `verorun-*.service` units, remove their files, then `daemon-reload` + `reset-failed`
2. Remove the Nginx `verorun.conf` config and reload nginx
3. Remove code directories and logs
4. Drop the `appdb` database, then the `app` role
   (lingering connections to the DB are terminated first, so the drop never
   blocks on the role-owner dependency)
5. Force-clean leftover processes (`gunicorn` workers / `health_check.sh`)
6. Final verification — if any VeroRun process or `verorun-*.service` file still
   exists the script prints `[FAIL] Uninstall INCOMPLETE` and exits non-zero
   (no more misleading "Server is clean" when it is not)

System packages (python3, nginx, postgresql, git) are **not** removed.

After this, you can run the install command again for a clean install.

---

## Architecture Overview

### Service Layout

```
Internet
    │
    ▼
  Nginx (port 80/443)
    │
    ├── /admin/* ──────────────► verorun-admin (:8084)
    ├── /auth/*, /subscribe ──► verorun-auth (:8083)
    └── /* ──────────────────► verorun-main (:8081)
```

### Subdomain Routing

| Subdomain | Port | WSGI App | Purpose |
|-----------|------|----------|---------|
| `yourdomain.com` | 8081 | `auth_server:app` | Main site, unified login, OAuth, user APIs |
| `platform.yourdomain.com` | 8083 | `main_site:app` | User console, subscriptions |
| `agent.yourdomain.com` | 8084 | `admin:app` | Admin panel, plugin management |

### File Locations

| Path | Purpose |
|------|---------|
| `/home/verorun/verorun/` | Application code |
| `/home/verorun/verorun/venv/` | Python virtual environment |
| `/home/verorun/verorun/.env` | Environment configuration |
| `/home/verorun/verorun/data/` | SQLite database (if used) |
| `/var/log/verorun/` | Service logs |
| `/run/verorun/` | Runtime status files (update status, health probe) |
| `/etc/systemd/system/verorun-*.service` | systemd service files |
| `/etc/nginx/sites-available/verorun.conf` | Nginx configuration |

---

## Post-Install Configuration

### 1. Seed Initial Data

`install` mode already runs migration + seed automatically (admin account, plans, products are created during install). Only re-run seed manually when needed — e.g. after `update` or to reset initial data:

```bash
sudo bash deploy/install.sh seed
```

Admin credentials are set interactively during installation via `prompt_admin_creds()`.
You will be prompted to enter a username and password. If not provided, a random
password is generated and displayed once — **save it immediately**.

**Non-interactive flags:** `--admin-user` and `--admin-pass` must be provided
**together**. Passing only one of them fails the install explicitly instead of
silently falling back to a random password.

**Important:** Change the admin password after first login.

### 2. Configure Domain (if skipped during install)

```bash
sudo bash deploy/install.sh configure-domain your-domain.com
```

This writes Nginx config, generates systemd service files, and restarts everything.

### 3. Set Up SSL with Let's Encrypt (Recommended)

After the domain is configured and DNS is pointing to your server:

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com -d www.your-domain.com -d platform.your-domain.com -d agent.your-domain.com
```

### 4. Configure API Keys

Edit `/home/verorun/verorun/.env` and replace placeholder API keys:

- `DASHSCOPE_TEXT_KEY` — DashScope (Alibaba AI) API key
- `OPENAI_API_KEY` — OpenAI API key
- `DEEPSEEK_API_KEY` — DeepSeek API key

After updating, restart services:

```bash
sudo bash deploy/install.sh restart
```

---

## Troubleshooting

### All services fail to start (exit code 1)

Check the common error:

```bash
journalctl -u verorun-main -n 50 --no-pager
```

**Most common cause:** The `platform/` directory name conflicts with Python's standard library `platform` module. This has been fixed by renaming to `main_site/`. If you are running an old version, update the code:

```bash
sudo bash deploy/install.sh update
```

### Service keeps restarting in a loop

```bash
journalctl -u verorun-auth -n 50 --no-pager | grep -A 20 "Traceback"
```

Common causes:
- Missing Python dependencies → run `install.sh update`
- Database connection failure → check PostgreSQL is running: `systemctl status postgresql`
- `.env` missing required keys → run `install.sh update` to fill missing keys

### Nginx fails to start

```bash
nginx -t
journalctl -u nginx -n 30 --no-pager
```

Ensure the domain is correctly configured in `.env`:
- `DEPLOY_DOMAIN=your-domain.com`

Then re-run configure-domain:

```bash
sudo bash deploy/install.sh configure-domain your-domain.com
```

### 502 Bad Gateway

This means Nginx is running but the backend service is not responding.

1. Check if the backend service is running:
   ```bash
   systemctl status verorun-main
   ```

2. Check the service logs:
   ```bash
   journalctl -u verorun-main -n 50 --no-pager
   ```

3. Most commonly the app module fails to import or a dependency is missing. Run `install.sh update` after fixing `.env` / dependencies.

### Rollback to previous version

```bash
sudo bash deploy/install.sh rollback
```

This reverts the code to the previous git commit and restarts all services.

### Git fetch hangs / never completes

The scripts disable interactive credential prompts (`GIT_TERMINAL_PROMPT=0`) and wrap
`git fetch` with a 60s timeout, so a bad remote fails fast instead of hanging. If `origin`
was changed to a mirror domain (`ghfast.top`, `ghproxy`, etc.), `ensure_git_auth` auto-corrects
it back to the official repository on the next run. To inspect / fix manually:

```bash
git -C /home/verorun/verorun remote -v
sudo git -C /home/verorun/verorun remote set-url origin https://github.com/fanjumin/verorun-pro.git
sudo bash deploy/install.sh update
```

---

## Manual Step-by-Step Installation

If the automated script fails, you can follow these manual steps.

### 1. System Dependencies

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python3-dev \
    nginx git curl wget build-essential libpq-dev libssl-dev postgresql postgresql-client
```

### 2. Create User

```bash
sudo useradd -m -s /bin/bash verorun
sudo mkdir -p /home/verorun/verorun /var/log/verorun /home/verorun/verorun/data
sudo chown -R verorun:verorun /home/verorun/verorun /var/log/verorun
```

### 3. Clone Code

**`verorun-pro` (public):**

```bash
sudo git clone -b master https://github.com/fanjumin/verorun-pro.git /home/verorun/verorun
sudo chown -R verorun:verorun /home/verorun/verorun
```

**`verorun-code` (private):**

```bash
sudo git clone -b master git@github.com:fanjumin/verorun-code.git /home/verorun/verorun
sudo chown -R verorun:verorun /home/verorun/verorun
```

### 4. Python Virtual Environment

```bash
sudo -u verorun python3 -m venv /home/verorun/verorun/venv
sudo -u verorun /home/verorun/verorun/venv/bin/pip install --upgrade pip
sudo -u verorun /home/verorun/verorun/venv/bin/pip install -r /home/verorun/verorun/requirements.txt
```

### 5. PostgreSQL Setup

```bash
sudo systemctl enable --now postgresql
sudo -u postgres psql -c "CREATE ROLE app WITH LOGIN PASSWORD 'change-me-in-production';"
sudo -u postgres psql -c "CREATE DATABASE appdb OWNER app;"
```

### 6. Generate .env

```bash
sudo bash -c 'cat > /home/verorun/verorun/.env << EOF
DEPLOY_MARKET=cn
DEPLOY_DOMAIN=your-domain.com
DB_PATH=/home/verorun/verorun/data/x7k2m9a4.db
PG_HOST=localhost
PG_PORT=5432
PG_DB=appdb
PG_USER=app
PG_PASSWORD=change-me-in-production
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ENCRYPTION_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
APP_MODE=main
PLUGIN_LICENSE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
CAPTCHA_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DEV_ACCOUNTS_ENCRYPTION_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
LICENSE_SERVER_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
PROBE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DASHSCOPE_TEXT_KEY=sk-your-key-here
OPENAI_API_KEY=sk-your-key-here
DEEPSEEK_API_KEY=sk-your-key-here
EOF'
sudo chmod 600 /home/verorun/verorun/.env
sudo chown verorun:verorun /home/verorun/verorun/.env
```

### 7. Create systemd Services

Run the script's service generator directly:

```bash
cd /home/verorun/verorun
# Manually create /etc/systemd/system/verorun-main.service
# Manually create /etc/systemd/system/verorun-auth.service
# Manually create /etc/systemd/system/verorun-admin.service
# Manually create /etc/systemd/system/verorun-health.service
# Manually create /etc/systemd/system/verorun-guardian.service
sudo systemctl daemon-reload
sudo systemctl enable verorun-main verorun-auth verorun-admin verorun-health verorun-guardian
sudo systemctl start verorun-main verorun-auth verorun-admin verorun-health verorun-guardian
```

### 8. Configure Nginx

Create `/etc/nginx/sites-available/verorun.conf` with the reverse proxy configuration (see the `write_nginx_config` function in `install.sh` for the template), then:

```bash
sudo ln -sf /etc/nginx/sites-available/verorun.conf /etc/nginx/sites-enabled/verorun.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx
```

---

## Release Signing（发布签名）

`deploy/lib/common.sh` 被三个入口脚本（`install.sh` / `install-code.sh` / `install-official.sh`）
在 curl|bash 一键安装时远程拉取，并通过内嵌的 SHA-256 pin 校验，防止 CDN / 仓库投毒。

**每次修改 `deploy/lib/common.sh` 后必须重新回填哈希**，否则发布后一键安装会因校验失败而损坏：

```bash
# 在仓库根目录运行（幂等）
python3 deploy/scripts/sign_release.py

# CI 门禁：校验全部 pin（含 EDU_COMMON_SHA256）是否与当前 common.sh 一致，不一致则退出码非 0
python3 deploy/scripts/sign_release.py --check
```

- 哈希按 **LF 归一化**计算（匹配 GitHub raw 内容；Windows CRLF 检出不影响）。
- `install.sh` 内含两个 pin：`COMMON_SHA256`（verorun-pro）与 `EDU_COMMON_SHA256`（verorun-edu），
  均由 `sign_release.py` 回填同一值（两仓库 common.sh 内容需同步）。
- `.github/workflows/tests.yml` 已接入 `sign_release.py --check` 作为发布质量门禁：
  若 common.sh 被修改但未回填，CI 直接失败。

---

## License

VeroRun Base is distributed under the [VeroRun Base EULA v1.0](../LICENSE). Copyright (c) 2024-2026 VeroRun AI. All rights reserved.
