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
- **pgvector（可选）:** 依赖向量检索的插件需要 PostgreSQL 的 pgvector 二进制
  （PGDG 仓库的 `postgresql-XX-pgvector`，XX=PG 主版本）。安装脚本缺失时**不中断**，
  仅提示安装指引；插件在激活时用 `CREATE EXTENSION IF NOT EXISTS vector` 按需建扩展，
  缺二进制则相关插件不可用。
- **earlyoom（可选）:** 若部署机运行 earlyoom 且配置 `--prefer python node`，
  内存压力下可能误杀部署进程。建议部署期间暂停 earlyoom 或放宽该策略。

---

## Selecting a Distribution

VeroRun is distributed through two repositories — pick the one that matches your deployment:

| Distribution | Repository | When to use |
|--------------|-----------|-------------|
| `verorun-pro` | `https://github.com/fanjumin/verorun-pro` (public) | Standard enterprise package, open download. Install by cloning the repo (see below). |
| `verorun-code` | `https://github.com/fanjumin/verorun-code` (private) | Official site / enterprise customization. Requires SSH access to the private repository. |

`verorun-pro` is generated automatically from `verorun-code` on every version tag by the `sync-to-pro` CI workflow. `install.sh` sets the `GIT_REPO` variable per distribution (HTTPS for `verorun-pro`, SSH for `verorun-code`), so `update` always pulls from the correct source.

### Official Edition（官方版）

Official Edition（官方版）是 `verorun-code` **私有仓库**的授权部署形态，仅通过官方渠道
（SSH 访问私有仓库 + 独立部署入口）进行，**本文档不覆盖其部署方式**。
普通用户请使用本仓库（`verorun-pro`）中的 `install.sh` 即可。

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

> **HTTPS 证书：** `website` 类型在**真实终端（TTY）**下会自动询问 Let's Encrypt 邮箱并签发证书；
> **无 TTY（CI / 纯管道）且未传 `--ssl-email` 时自动跳过签发，降级为 HTTP**（SSO 无 Secure 标记）。
> 无 TTY 环境要拿 HTTPS，需显式传邮箱：
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh | sudo env INSTALL_TYPE=website bash -s -- install your-domain.com --ssl-email you@example.com
> ```

**Supported INSTALL_TYPE values:**

| Value | DEPLOY_TYPE | Description | Old Script |
|-------|-------------|-------------|------------|
| `website` | production | Domain + HTTPS, public deployment | `install.sh` |
| `professional` | lan | No domain, LAN access, verorun-pro | `install-local.sh` |
| `development` | dev | No plugins, verorun-code (SSH), requires deploy key | `install-dev.sh` |
| `educational` | edu | No domain, edu license required | (new) |

> `install-code.sh` is preserved as an independent shortcut for Team (code) deployments (full plugins).

**中国内地网络提示：** `install.sh` 默认 `GIT_REPO=https://github.com/fanjumin/verorun-pro.git`
（HTTPS 公开仓，浅克隆 `--depth 1`）；仅 `development`（SSH `git@github.com:fanjumin/verorun-code.git`）
与 `educational`（HTTPS verorun-edu）类型切换仓库。内地服务器直连 GitHub 传输易中途被掐断
（`fetch-pack: unexpected disconnect`）——HTTPS 克隆会自动回退到
ghfast.top / ghproxy.net 镜像；也可改用 gitee 源
（`GIT_REPO=git@gitee.com:fanjumin/verorun-code.git`，需配置 deploy key）、本地 bundle
中继或加大重试参数（`GIT_CLONE_ATTEMPTS` / `GIT_TIMEOUT`），详见下方 Troubleshooting 的
「中国内地网络：安装克隆 GitHub 失败」小节。另请确保部署脚本为 2026-08-27 之后版本
（旧版克隆参数为 60s × 2 次，新版默认 120s × 3 次并含多项修复）。


### Educational Edition（教育版）

```bash
# One-command edu install (no domain, edu license code required)
curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/install.sh | sudo env INSTALL_TYPE=educational bash
```

> **部署码（EDU_CODE）：** 有真实终端（TTY）时会交互式提示输入部署码（ED-XXXX）。
> **无 TTY（CI / 纯管道）时必须显式传 `EDU_CODE`**，否则脚本尝试从 `/dev/tty` 读取会失败退出：
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/install.sh \
>   | sudo env INSTALL_TYPE=educational EDU_CODE=ED-XXXX bash
> ```
> 测试部署码 `TEST-DEV-0001` / `EDU-TEST-0001` 仅放行开发/测试环境，不用于生产。

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

> **关于 `verorun-guardian`：** 非官方版（标准版）若缺少 Nuitka 编译产物
> `veroguard/dist/verorun-guardian.bin`（`dist/` 不入库，标准版安装后必然缺失），
> 脚本只写 unit 文件但**不 enable**（避免 systemd 崩溃循环），此时实际启动的服务为
> **4 个**（main / auth / admin / health）。补齐二进制后手动启用：
> `sudo systemctl enable --now verorun-guardian`。

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

### LAN / Professional 模式（无域名）

- 单 server 代理：`/admin/` → :8084、`/auth/`、`/subscribe` → :8083、`/` → :8081；
  用户登录/控制台入口为 `http://<内网IP>/login`。
- 内置插件：email / sms / im_gateway / site_domains / `_base`（共享依赖）随安装稀疏检出
  （2026-08-30 F-1/F-1a 修复），"Bundled plugin tables" 建表步骤可正常执行，管理员可直接在后台启用这三个内置插件。
- HTTPS 补齐（可选）：`sudo bash deploy/intranet/setup_lan_selfsigned.sh`
  （生成 IP-SAN 自签证书 + 启用 nginx 443 + 置 `DEPLOY_PROTOCOL=https`；成功路径退出码 0 并打印访问地址与重启服务提示）。

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

### Deployment log stops mid-step / no output

Unattended runs via `nohup bash deploy/install.sh ... > install.log 2>&1 &` can end
abruptly and hide the real failure. Since the 2026-08-27 fix the scripts print the
failing step and exit code on `set -e` aborts; for real-time line-buffered output use
a pty or `stdbuf`:

```bash
# pty 行缓冲：崩溃时保留真实错误与退出码
sudo script -qec "bash deploy/install.sh install your-domain.com" install.log
# 或：stdbuf 行缓冲
stdbuf -oL -eL sudo bash deploy/install.sh install your-domain.com 2>&1 | tee install.log
```

Always capture the **exit code** right after the run (e.g. `echo $?`); `set -e` exits
silently if the failing command swallowed its own stderr.

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

### 中国内地网络：安装克隆 GitHub 失败（fetch-pack unexpected disconnect）

内地服务器直连 `git@github.com:fanjumin/verorun-code.git` 拉取约 60MB 仓库（约 9500 个对象）时，
常见 `fetch-pack: unexpected disconnect while reading sideband packet`：日志已出现
`remote: Enumerating objects … Counting 100%` 说明 SSH 认证与仓库访问均正常，是**传输中途被
国际链路/GFW 掐断**，属网络层问题，而非脚本或权限问题。

处置（按推荐顺序）：

1. **换 gitee 源**（国内直连，推荐）：在服务器生成密钥并把公钥加入 gitee 仓库「部署公钥」后：

   ```bash
   sudo env GIT_REPO=git@gitee.com:fanjumin/verorun-code.git bash deploy/install.sh install
   ```

2. **本地 bundle 中继**（无外网环境最稳；2026-08-30 已在内网两台机器实测，一次成功）：
   在任一可访问仓库的机器上打包，上传到目标服务器后建立本地裸仓，让安装器直接使用：

   ```bash
   git bundle create verorun-code.bundle refs/heads/master                # 约 62MB
   git clone --bare verorun-code.bundle /home/guxiao/verorun-code.git     # 服务器上建本地源
   sudo env GIT_REPO=/home/guxiao/verorun-code.git bash deploy/install.sh install
   ```

3. **加大重试参数后低峰期重试**（`GIT_CLONE_ATTEMPTS` / `GIT_TIMEOUT` 可用环境变量覆盖，
   默认 120s × 3 次；2026-08-27 `316ec5c8` 之前的旧版脚本为 60s × 2 次，对 60MB 级仓库
   明显不足，请先更新部署脚本）：

   ```bash
   sudo env GIT_CLONE_ATTEMPTS=5 GIT_TIMEOUT=300 bash deploy/install.sh install
   ```

4. **预克隆**：先手动把仓库克隆到 `APP_HOME`，安装器检测到已有 `.git` 会走
   `fetch/reset` 路径，跳过克隆步骤：

   ```bash
   git clone --depth 1 git@gitee.com:fanjumin/verorun-code.git /home/guxiao/verorun
   sudo bash deploy/install.sh install
   ```

注意：失败提示中的 `https_proxy` 仅对 HTTPS 协议克隆生效；`git@github.com` 走 SSH 22 端口，
如需经代理克隆应使用
`GIT_SSH_COMMAND="ssh -o ProxyCommand='nc -X connect -x <代理>:<端口> %h %p'"`，或改用
HTTPS + token 克隆。


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

> **pgvector:** 依赖向量检索的插件在激活时执行
> `CREATE EXTENSION IF NOT EXISTS vector SCHEMA public`（幂等）。未安装 pgvector 二进制时该扩展失败，
> 安装：`sudo apt-get install postgresql-XX-pgvector`（XX=PG 主版本，如 14）。
> 自动部署脚本检测到 `vector.control` 时写入 `trusted = true`，使 `app` 角色无需超级权限即可建扩展；
> 缺失时仅提示，不中断安装。

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

`deploy/lib/common.sh` 被入口脚本（`install.sh` / `install-code.sh`）
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
