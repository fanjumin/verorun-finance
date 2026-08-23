#!/bin/bash
# ==========================================================================
# VeroRun — Uninstall script
# ==========================================================================
# Usage: sudo bash deploy/uninstall.sh
# Mirrors every resource created by install.sh install, reverses them.
# Does NOT remove system packages (python3/nginx/postgresql/git).
# ==========================================================================
set -euo pipefail

APP_USER="${SUDO_USER:-$(whoami)}"
APP_HOME="${VR_APP_HOME:-}"
LOG_DIR="/var/log/verorun"
SERVICE_DIR="/etc/systemd/system"

# 审计 H-3 修复：install supports customizing APP_HOME via environment variables; uninstall must not hardcode the default path.
# Resolve the actual WorkingDirectory from the systemd service file first; fall back to the default path only on failure.
if [ -z "${APP_HOME}" ] && [ -f "${SERVICE_DIR}/verorun-main.service" ]; then
    APP_HOME=$(grep '^WorkingDirectory=' "${SERVICE_DIR}/verorun-main.service" 2>/dev/null | head -1 | cut -d= -f2)
fi
APP_HOME="${APP_HOME:-/home/${APP_USER}/verorun}"

# ── Colors ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; BLUE='\033[0;34m'; NC='\033[0m'
OK="${GREEN}[OK]${NC}"; WARN="${YELLOW}[WARN]${NC}"; FAIL="${RED}[FAIL]${NC}"; INFO="${BLUE}[i]${NC}"
step() { echo -e "\n${BLUE}═══ $1 ═══${NC}"; }
done_step() { echo -e "${OK} $1"; }

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${FAIL} Please run with sudo: sudo bash deploy/uninstall.sh"
    exit 1
fi

echo ""
echo -e "${RED}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${RED}║  WARNING: This will remove ALL VeroRun data & services.  ║${NC}"
echo -e "${RED}║  This action is IRREVERSIBLE. Databases will be DROPPED. ║${NC}"
echo -e "${RED}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
# 审计 M8 修复：non-interactive one-shot uninstall (curl | sudo bash) must support VR_UNINSTALL_YES=1 to skip confirmation;
# Without a TTY and explicit authorization, exit with a clear error (no silent hang of read </dev/tty under a pipe).
if [ "${VR_UNINSTALL_YES:-0}" = "1" ]; then
    echo -e "${INFO} VR_UNINSTALL_YES=1 — skipping confirmation"
elif [ -t 0 ]; then
    read -r -p "  Type 'yes' to confirm: " CONFIRM || CONFIRM=""
    if [ "${CONFIRM:-}" != "yes" ]; then
        echo -e "${INFO} Aborted."
        exit 0
    fi
else
    echo -e "${FAIL} Non-interactive uninstall requires VR_UNINSTALL_YES=1"
    exit 1
fi

# 1. systemd services (reverse of write_systemd_services + restart_services)
step "systemd services"
for svc in verorun-admin verorun-auth verorun-main verorun-health verorun-guardian; do
    systemctl stop "${svc}" 2>/dev/null || true
    systemctl disable "${svc}" 2>/dev/null || true
    echo "  stop/disable: ${svc}"
done
rm -f "${SERVICE_DIR}"/verorun-*.service
systemctl daemon-reload
done_step "systemd services removed"

# 2. Nginx config (reverse of write_nginx_config)
step "Nginx config"
rm -f /etc/nginx/sites-available/verorun.conf
rm -f /etc/nginx/sites-enabled/verorun.conf
if systemctl is-active --quiet nginx 2>/dev/null; then
    systemctl reload nginx 2>/dev/null || true
    echo "  nginx reloaded"
fi
done_step "Nginx config removed"

# 3. Directories (reverse of mkdir; do NOT touch the login user)
step "User & files"
# 审计 M8 修复：APP_USER is the SSH login user (e.g. ***REMOVED***); the install script never creates a system user,
# so the former userdel would wrongly delete the login account and lock out SSH. Uninstall only cleans directories created by the install script.
rm -rf "${LOG_DIR}" 2>/dev/null || true
rm -rf "${APP_HOME}" 2>/dev/null || true
done_step "User & directories cleaned"

# 4. PostgreSQL (reverse of CREATE ROLE + CREATE DATABASE)
step "PostgreSQL"
# 审计 M8 修复：terminate lingering connections to appdb first (DROP DATABASE fails while service/guardian processes hold it),
# Check the DROP result explicitly and print executable manual repair commands instead of swallowing errors silently.
sudo -u postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='appdb' AND pid <> pg_backend_pid()" >/dev/null 2>&1 || true
if sudo -u postgres psql -c "DROP DATABASE IF EXISTS appdb" 2>&1; then
    done_step "Database appdb dropped"
else
    echo -e "${FAIL} DROP DATABASE appdb failed — check lingering connections:"
    echo -e "${INFO}   sudo -u postgres psql -c \"SELECT pid, query FROM pg_stat_activity WHERE datname='appdb'\""
    echo -e "${INFO}   sudo -u postgres psql -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='appdb'\""
    echo -e "${INFO}   sudo -u postgres psql -c \"DROP DATABASE appdb\""
fi
# (2026-08-21) site_builder 独立数据库豁免已取消，无独立库需 DROP。
if sudo -u postgres psql -c "DROP ROLE IF EXISTS app" 2>&1; then
    done_step "Role app dropped"
else
    echo -e "${FAIL} DROP ROLE app failed — manual command:"
    echo -e "${INFO}   sudo -u postgres psql -c \"DROP ROLE app\""
fi

# 5. Residual processes — systemd stop 超时或 worker 幸存时的兜底清理
step "Residual processes"
# 审计 F1 修复：systemctl stop 默认仅等待 15s，gunicorn worker / health_check.sh 可能幸存；
# pgrep 兜底清理，先温和 kill，若 2s 后仍未退出则 kill -9。
for _pat in "${APP_HOME}" "gunicorn" "health_check.sh"; do
    _pids=$(pgrep -f "${_pat}" 2>/dev/null || true)
    if [ -n "${_pids}" ]; then
        echo "  leftover '${_pat}': ${_pids}"
        if ! echo "${_pids}" | xargs -r kill 2>/dev/null; then
            sleep 2
            pgrep -f "${_pat}" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
            echo -e "${WARN} force-killed leftover: ${_pat}"
        fi
    fi
done
done_step "Residual processes cleaned"

# 6. systemd again: clear failed / lingering unit state after service files are gone
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true
done_step "systemd state reset"

# 7. Residual config files (sudoers + guardian env)
step "Config files"
rm -f /etc/default/verorun-guardian /etc/sudoers.d/verorun
done_step "Config files removed"

step "Verify"
_issue=0
if pgrep -f "${APP_HOME}" >/dev/null 2>&1; then
    echo -e "${FAIL} VeroRun processes still running"
    _issue=1
fi
if ls /etc/systemd/system/verorun-*.service >/dev/null 2>&1; then
    echo -e "${FAIL} verorun-*.service files still present"
    _issue=1
fi
if [ "${_issue}" = "1" ]; then
    echo -e "${FAIL} Uninstall INCOMPLETE — see messages above"
    exit 1
fi
done_step "Nothing left; server clean for fresh install"
echo ""
echo -e "${INFO} System packages (python3, nginx, postgresql, git) are NOT removed."
echo -e "${INFO} Ready for fresh install:"
echo -e "${INFO}   git clone https://github.com/fanjumin/verorun-pro.git"
echo -e "${INFO}   cd verorun-pro"
echo -e "${INFO}   sudo bash deploy/install.sh install your-domain.com"
