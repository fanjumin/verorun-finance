#!/bin/bash
# ==========================================================================
# VeroRun — One-command unified deploy script (build-2026.08.11)
# ==========================================================================
# Usage:
#   # Interactive (recommended)
#   sudo bash deploy/install.sh install
#
#   # CI / automation (environment variable)
#   sudo env INSTALL_TYPE=professional bash deploy/install.sh install
#
#   # curl|bash one-command
#   curl -fsSL https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/install.sh | sudo env INSTALL_TYPE=professional bash
#
#   sudo bash deploy/install.sh update           # update code, deps, and restart
#   sudo bash deploy/install.sh restart          # restart services only
#   sudo bash deploy/install.sh health           # health check
#   sudo bash deploy/install.sh rollback         # rollback to previous commit
#   sudo bash deploy/install.sh seed             # seed initial data (admin, plans, products)
#   sudo bash deploy/install.sh configure-domain  # configure domain post-install
#   --approve-migrate: explicitly approve DB migration + seed on install
#   --skip-deps: skip system + Python dependency installation (existing env re-deploy)
#   --region=cn|global: region routing (default global)
#
# Supported INSTALL_TYPE values: website | professional | development | educational
#   website      → DEPLOY_TYPE=production (domain + HTTPS)
#   professional → DEPLOY_TYPE=lan         (no domain, LAN access)
#   development  → DEPLOY_TYPE=dev         (verorun-code SSH, no plugins)
#   educational  → DEPLOY_TYPE=edu         (no domain, edu license)
#
# install-code.sh is the team full-plugin (code) shortcut; Development maps to dev (no plugins).
# install-local.sh and install-dev.sh have been removed (logic merged into Professional / Development).
# ==========================================================================
set -euo pipefail

# ── Default config ────────────────────────────────────────────────────
: "${GIT_REPO:=https://github.com/fanjumin/verorun-pro.git}"
: "${GIT_BRANCH:=master}"
: "${APP_USER:=${SUDO_USER:-$(whoami)}}"
# home is /root when APP_USER=root (not /home/root), consistent with install-code.sh / install-dev.sh
if [ "${APP_USER}" = "root" ]; then
    : "${APP_HOME:=/root/verorun}"
else
    : "${APP_HOME:=/home/${APP_USER}/verorun}"
fi
: "${VENV_DIR:=${APP_HOME}/venv}"
: "${LOG_DIR:=/var/log/verorun}"
: "${SERVICE_DIR:=/etc/systemd/system}"
: "${DOMAIN:=}"
: "${REGION:=global}"                # cn | global
: "${DEPLOY_TYPE:=production}"       # production | lan | code | dev | edu — default; select_deploy_type may override
: "${VR_ADMIN_USERNAME:=}"
: "${VR_ADMIN_PASSWORD:=}"
: "${SSL_EMAIL:=}"

# ── Load shared function library (lib/common.sh: logging / CN network adaptation / git / systemd / health check, etc.) ──
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
fi
if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/lib/common.sh" ]; then
    # Executed from a real file (e.g., after git clone) → load directly
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/lib/common.sh"
else
    # One-command install (curl | sudo bash): script runs from stdin, no real path
    # → fetch the shared library from verorun-pro (public repo, matching the one-command install link) into a temp file and load it
    # 审计 D10: source is fixed to verorun-pro (verorun-code is a private repo, inaccessible to anonymous users);
    # Verify against a SHA-256 allowlist after fetching, to guard against CDN/repo poisoning.
    # 审计 F-02：教育版（INSTALL_TYPE=educational）从 verorun-edu 拉取 common.sh 并用其哈希校验。
    # 注意：EDU_COMMON_SHA256 与 COMMON_SHA256 默认 pin 同一哈希——两仓库 common.sh 必须由 CI
    # （sync-to-pro / sync-to-edu 的 git archive）同步保持一致，否则教育版一键安装会因哈希不匹配而失败；
    # 如需独立 pin，可用 EDU_COMMON_SHA256 环境变量覆盖。
    if [ "${INSTALL_TYPE:-}" = "educational" ]; then
        _COMMON_REMOTE="${EDU_COMMON_REMOTE:-https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/lib/common.sh}"
        _COMMON_MIRROR="${EDU_COMMON_MIRROR:-https://ghfast.top/https://raw.githubusercontent.com/fanjumin/verorun-edu/master/deploy/lib/common.sh}"
        # Computed and backfilled at release time by deploy/scripts/sign_release.py (LF-normalized hash)
        _COMMON_SHA256="${EDU_COMMON_SHA256:-575ef745dc9e4a84cf9f9bdee68756e2fe9e4bd54b77290d77cb21013faccf9a}"
    else
        _COMMON_REMOTE="${COMMON_REMOTE:-https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/lib/common.sh}"
        _COMMON_MIRROR="${COMMON_MIRROR:-https://ghfast.top/https://raw.githubusercontent.com/fanjumin/verorun-pro/master/deploy/lib/common.sh}"
        # Computed and backfilled at release time by deploy/scripts/sign_release.py (LF-normalized hash)
        _COMMON_SHA256="${COMMON_SHA256:-575ef745dc9e4a84cf9f9bdee68756e2fe9e4bd54b77290d77cb21013faccf9a}"
    fi
    _tmp_common="$(mktemp)"
    # Audit P3-2: clean up the temp file on Ctrl+C interruption
    trap 'rm -f "${_tmp_common}"' EXIT
    _ok=0
    _fetch_common() {
        # Audit D10: jsdelivr CDN preferred (reachable in China; raw.githubusercontent.com is often blocked by GFW and times out);
        # After a successful fetch, verify against the SHA-256 allowlist; on failure, automatically fall back to the next source.
        local _url="$1"
        if command -v curl >/dev/null 2>&1; then
            curl -sSL --connect-timeout 5 --max-time 15 --retry 2 --retry-delay 2 "${_url}" -o "${_tmp_common}" 2>/dev/null
        elif command -v wget >/dev/null 2>&1; then
            wget -q --timeout=15 --tries=3 -O "${_tmp_common}" "${_url}" 2>/dev/null
        else
            return 1
        fi
    }
    _verify_common() {
        local _actual_sha=""
        if command -v sha256sum >/dev/null 2>&1; then
            _actual_sha="$(sha256sum "${_tmp_common}" | awk '{print $1}')"
        elif command -v python3 >/dev/null 2>&1; then
            _actual_sha="$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "${_tmp_common}")"
        fi
        [ -n "${_actual_sha}" ] && [ "${_actual_sha}" = "${_COMMON_SHA256}" ]
    }
    # Order: CDN mirror first → official source as fallback (SHA-256 verified for every source)
    for _src in "${_COMMON_MIRROR}" "${_COMMON_REMOTE}"; do
        if _fetch_common "${_src}" && _verify_common; then
            _ok=1
            break
        fi
    done
    if [ "${_ok}" != "1" ]; then
        echo "FATAL: cannot fetch deploy/lib/common.sh (check network, or use the git clone method)" >&2
        echo "  INFO: common.sh signature mismatch often means it was edited without re-signing." >&2
        echo "  INFO: Maintainer: run 'python3 deploy/scripts/sign_release.py' and commit." >&2
        rm -f "${_tmp_common}"
        exit 1
    fi
    # shellcheck disable=SC1090
    source "${_tmp_common}"
    rm -f "${_tmp_common}"
fi

# ══════════════════════════════════════════════════════════════════════
# Deploy type resolution (unified entry, build-2026.08.11)
# DEPLOY_TYPE: production | lan | code | edu
# ══════════════════════════════════════════════════════════════════════
select_deploy_type() {
    # ================================================================
    # install mode: always go through the menu / environment variable, regardless of whether .env exists
    # Even if a leftover .env from a failed install exists, the selection menu is shown again
    # ================================================================
    if [ "${DEPLOY_MODE}" = "install" ]; then
        # 1) Environment variable takes precedence (CI / curl|bash pipe)
        if [ -n "${INSTALL_TYPE:-}" ]; then
            case "${INSTALL_TYPE}" in
                website)      DEPLOY_TYPE="production" ;;
                professional) DEPLOY_TYPE="lan" ;;
                development)  DEPLOY_TYPE="dev" ;;
                educational)  DEPLOY_TYPE="edu" ;;
                *) echo -e "${FAIL} Unknown INSTALL_TYPE: ${INSTALL_TYPE}"; exit 1 ;;
            esac
            echo -e "${INFO} Deploy type from INSTALL_TYPE: ${DEPLOY_TYPE}"
            return
        fi
        # 2) Interactive menu — always shown on every install
        # Menu read uses /dev/tty (real terminal); remains interactive even under a curl|bash pipe
        # 审计 S-7：无 TTY（CI / 纯管道）时优雅退出，提示用 INSTALL_TYPE 环境变量，避免 read 读到 EOF 后误报 Invalid choice
        if ! { exec 3<>/dev/tty; } 2>/dev/null; then
            echo -e "${FAIL} No interactive terminal available — pass INSTALL_TYPE explicitly:"
            echo -e "${INFO}   curl ... | sudo env INSTALL_TYPE=professional bash"
            echo -e "${INFO}   INSTALL_TYPE: website | professional | development | educational"
            exit 1
        fi
        exec 3>&-
        echo ""
        echo "  VeroRun installer wizard - select a deployment type"
        echo "  ----------------------------------------------"
        echo "  [1] Website        Production (requires domain + HTTPS)"
        echo "  [2] Professional   Pro edition (no domain, LAN access)"
        echo "  [3] Development    Dev edition (verorun-code, no plugins, requires SSH key)"
        echo "  [4] Educational    Edu edition (no domain, requires edu license code)"
        echo -n "  Enter your choice [1-4]: " > /dev/tty
        read -r _choice < /dev/tty
        case "${_choice}" in
            1) DEPLOY_TYPE="production" ;;
            2) DEPLOY_TYPE="lan" ;;
            3) DEPLOY_TYPE="dev" ;;
            4) DEPLOY_TYPE="edu" ;;
            *) echo -e "${FAIL} Invalid choice, please try again"; exit 1 ;;
        esac
        echo -e "${INFO} Deploy type selected: ${DEPLOY_TYPE}"
        return
    fi

    # ================================================================
    # Non-install modes (update / restart / health / rollback / seed, etc.):
    # Read from .env, no interaction
    # ================================================================

    # Installed environment: read DEPLOY_TYPE from .env (idempotent)
    if [ -f "${APP_HOME}/.env" ] && grep -q "^DEPLOY_TYPE=" "${APP_HOME}/.env"; then
        DEPLOY_TYPE=$(grep "^DEPLOY_TYPE=" "${APP_HOME}/.env" | tail -1 | cut -d= -f2)
        echo -e "${INFO} Deploy type from .env: ${DEPLOY_TYPE}"
        return
    fi

    # Legacy .env compatibility: infer from DEPLOY_DOMAIN when DEPLOY_TYPE is absent
    # (Domain present -> production; no domain -> lan. This logic only applies to existing environments)
    if [ -f "${APP_HOME}/.env" ]; then
        local _old_dom
        _old_dom=$(grep "^DEPLOY_DOMAIN=" "${APP_HOME}/.env" 2>/dev/null | tail -1 | cut -d= -f2)
        if [ -n "${_old_dom}" ]; then
            DEPLOY_TYPE="production"
        else
            DEPLOY_TYPE="lan"
        fi
        echo -e "${INFO} Inferred DEPLOY_TYPE=${DEPLOY_TYPE} (pre-DEPLOY_TYPE .env)"
        return
    fi

    # Fallback: if there is no .env at all (edge case, almost unreachable outside install mode)
    echo -e "${WARN} No .env found — defaulting DEPLOY_TYPE=${DEPLOY_TYPE}"
}

# ══════════════════════════════════════════════════════════════════════
# Apply deploy type: adjust GIT_REPO / SPARSE_DIRS etc. based on DEPLOY_TYPE
# ══════════════════════════════════════════════════════════════════════
apply_deploy_type() {
    case "${DEPLOY_TYPE}" in
        dev)
            # Developer edition: verorun-code over SSH, plugins EXCLUDED (requirement "dev = no plugins").
            # F-10: GIT_REPO env-injectable — a user-provided value (≠ script default) is respected.
            # 审计 2026-08-15：短路赋值 `[ ] && assign` 在 set -e 下当 GIT_REPO 非默认值（如离线本地 bare）时
            # 会因 [ ] 返回非零导致整条语句退出码非零 → set -e 静默退出安装。改写为 if 形式，行为等价但防截断。
            if [ "${GIT_REPO}" = "https://github.com/fanjumin/verorun-pro.git" ]; then
                GIT_REPO="git@github.com:fanjumin/verorun-code.git"
            fi
            ;;
        code)
            # Team full-plugin edition (install-code.sh) + back-compat: existing code-type .env updates.
            # F-10: env-injectable — a user-provided value (≠ script default) is respected.
            # 审计 2026-08-15：与 dev 分支同源加固（见上）。
            if [ "${GIT_REPO}" = "https://github.com/fanjumin/verorun-pro.git" ]; then
                GIT_REPO="git@github.com:fanjumin/verorun-code.git"
            fi
            SPARSE_DIRS="${SPARSE_DIRS:-} plugins"
            ;;
        edu)
            # Educational edition: verorun-edu over HTTPS, no domain, bundled plugins
            GIT_REPO="https://github.com/fanjumin/verorun-edu.git"
            # 全量检出（含全部插件）——教育版需运行全部插件（VeroScholar 等）。
            # 2026-08-20：sparse 白名单漏 plugins/ 曾导致插件被清空、蓝图挂载失败、
            # 菜单点击落入 SPA catch-all 出现重叠 Admin。教育版必须全量。
            SPARSE_DIRS=""
            ;;
    esac
}

# ── Mode / Domain detection ──────────────────────────────────────────

detect_domain() {
    # Audit P0-1: removed the flag-prefix check. The function is called only after the while loop has finished parsing arguments,
    # The domain in the positional argument is already captured into DOMAIN by the while loop's *) catchall.
    if [ -n "${1:-}" ]; then
        DOMAIN="$1"
    elif [ -z "${DOMAIN:-}" ] && [ -f "${APP_HOME}/.env" ]; then
        DOMAIN=$(grep "^DEPLOY_DOMAIN=" "${APP_HOME}/.env" 2>/dev/null | tail -1 | cut -d= -f2)
    fi
}

# Audit M18: FQDN format validation for domains (rejects scheme/path/port/spaces/consecutive dots/leading or trailing hyphens)
_is_valid_fqdn() {
    local d="$1"
    case "${d}" in
        *://*|*/*|*:*|*" "*|*..*|.*|*.-*|*-.*) return 1 ;;
    esac
    echo "${d}" | grep -qE '^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'
}

prompt_domain() {
    if [ -n "${DOMAIN}" ]; then
        return
    fi
    echo -e "${INFO} Domain is required to continue."
    # Prompt uses echo -n > /dev/tty — read -p writes the prompt to stderr,
    # and under a 2>&1 | tail pipe it gets swallowed by buffering, making the prompt appear hung.
    echo -n "  Enter your domain (e.g., verorun.com) — leave empty to configure later: " > /dev/tty
    read -r DOMAIN < /dev/tty
    DOMAIN="$(echo "${DOMAIN}" | tr -d '[:space:]')"
    if [ -z "${DOMAIN}" ]; then
        echo -e "${WARN} Domain skipped. Run after install:"
        echo -e "${INFO}   sudo bash deploy/${INSTALL_SCRIPT} configure-domain <your-domain>"
    elif _is_valid_fqdn "${DOMAIN}"; then
        echo -e "${OK} Domain set to: ${DOMAIN}"
    else
        echo -e "${WARN} Invalid domain format (${DOMAIN}), skipped. Use a valid FQDN such as verorun.com"
        DOMAIN=""
    fi
}

# ==========================================================================
# Fresh install — Audit C-1: do_install unified into lib/common.sh (driven by DEPLOY_TYPE)
# ==========================================================================

# ==========================================================================
# Incremental update — Audit C-1: do_update unified into lib/common.sh (driven by DEPLOY_TYPE)
# ==========================================================================

# ==========================================================================
# Configure domain post-install
# ==========================================================================
do_configure_domain() {
    local domain="$1"
    if [ -z "$domain" ]; then
        echo -e "${FAIL} Usage: sudo bash deploy/install.sh configure-domain <your-domain>"
        exit 1
    fi
    # Audit M18: FQDN validation for the configure-domain argument; reject immediately if invalid
    if ! _is_valid_fqdn "${domain}"; then
        echo -e "${FAIL} Invalid domain: ${domain} (should be a valid FQDN, e.g., verorun.com)"
        exit 1
    fi

    step "Configure domain: ${domain}"

    local env_file="${APP_HOME}/.env"
    if [ ! -f "${env_file}" ]; then
        echo -e "${FAIL} .env not found. Run '${INSTALL_SCRIPT} install' first."
        exit 1
    fi

    # Update DEPLOY_DOMAIN in .env
    if grep -q "^DEPLOY_DOMAIN=" "${env_file}"; then
        local _esc=$(printf '%s' "${domain}" | sed 's/[\/&\\]/\\&/g')
        sed -i "s/^DEPLOY_DOMAIN=.*/DEPLOY_DOMAIN=${_esc}/" "${env_file}"
    else
        echo "DEPLOY_DOMAIN=${domain}" >> "${env_file}"
    fi
    DOMAIN="$domain"
    done_step "Updated DEPLOY_DOMAIN in .env"

    step "systemd services"
    chmod +x "${APP_HOME}/deploy/health_check.sh" 2>/dev/null || true
    write_systemd_services
    done_step "systemd services configured"

    step "Nginx"
    write_nginx_config
    nginx -t && systemctl restart nginx
    done_step "Nginx configured"

    # Audit D3: TLS liveness check after configure-domain (cert present → probe 443; absent → suggest issuance command)
    step "TLS check"
    local _cert_dir="/etc/letsencrypt/live/${domain}"
    if [ -f "${_cert_dir}/fullchain.pem" ] && [ -f "${_cert_dir}/privkey.pem" ]; then
        if command -v curl >/dev/null 2>&1; then
            local _tls_code
            _tls_code=$(curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "https://${domain}/" 2>/dev/null || echo "000")
            if [ "${_tls_code}" = "000" ]; then
                echo -e "${WARN} HTTPS probe failed (HTTP ${_tls_code}); check that port 443 is open and the certificate is valid"
            else
                done_step "TLS OK (https://${domain} → HTTP ${_tls_code})"
            fi
        else
            done_step "Certificate present (curl not installed, skipped online probe)"
        fi
    else
        echo -e "${WARN} Certificate not found: currently plain HTTP. To enable HTTPS, run:"
        echo "  sudo certbot --nginx -d ${domain} -d www.${domain} -d platform.${domain} -d agent.${domain}"
    fi

    step "Start services"
    restart_services
    done_step "Services started"

    print_summary
}

# ==========================================================================
# .env management — Audit C-1: generate_env unified into lib/common.sh (driven by DEPLOY_TYPE)
# ==========================================================================

# ==========================================================================
# Nginx — Audit C-1: write_nginx_config unified into lib/common.sh (driven by DEPLOY_TYPE)
# ==========================================================================

# ==========================================================================
# Summary — Audit C-1: print_summary unified into lib/common.sh (driven by DEPLOY_TYPE)
# ==========================================================================

# ── Educational license placeholder (interface fixed)────────────────────────────────
# Future email verification / CN plugin licensing plugs in here; the deploy layer only performs ED-code + check validation
_edu_license_check() {
    if [ "${DEPLOY_TYPE}" != "edu" ] || [ "${DEPLOY_MODE}" != "install" ]; then
        return 0
    fi

    echo -e "${INFO} Educational license - enter your edu deployment code (ED-XXXX)"
    if [ -z "${EDU_CODE:-}" ]; then
        echo -n "  Deployment code: " > /dev/tty
        read -r EDU_CODE < /dev/tty
    else
        echo -e "${INFO}  Deployment code (from env EDU_CODE): ${EDU_CODE}"
    fi
    EDU_CODE="${EDU_CODE// /}"
    if [ -z "${EDU_CODE}" ]; then
        echo -e "${FAIL} Educational deployment code must not be empty"; exit 1
    fi
    # 审计 TEST-KEY：教育版测试部署码白名单 —— 仅供开发/测试环境放行本次校验，
    # 不经过云端 is_valid 校验，也不写入正式部署用途。其他任意码照旧走云端校验。
    case "${EDU_CODE}" in
        TEST-DEV-0001|EDU-TEST-0001)
            echo -e "${WARN} [TEST] Educational TEST key accepted: ${EDU_CODE} (dev/test only, NOT for production)"
            export EDU_CODE
            return 0
            ;;
    esac
    # Region-aware validation endpoint (follows license_service's region routing convention);
    # 审计 F5 修复：EDU_LICENSE_ENDPOINT 允许离网/内网部署覆盖云端端点
    local _edu_url="${EDU_LICENSE_ENDPOINT:-}"
    if [ -z "${_edu_url}" ]; then
        case "${REGION}" in
            cn)     _edu_url="https://api.verorun.cn" ;;
            *)      _edu_url="https://api.verorun.com" ;;
        esac
    fi
    # 审计 F5 修复：校验前先做端点可达性预检，不可达时显式告警并给出降级指引（而非运行期静默失败）
    if ! curl -fsS --connect-timeout 6 --max-time 12 "${_edu_url}/health" >/dev/null 2>&1; then
        echo -e "${WARN} Edu license endpoint unreachable: ${_edu_url}"
        echo -e "${WARN}   - 离网/内网部署请通过 EDU_LICENSE_ENDPOINT 指定内网 license 服务地址"
        echo -e "${WARN}   - 或先配置 DNS / 放行出网后再安装，以免许可证校验失败"
    fi
    local _edu_check
    _edu_check=$(curl -fsSL --connect-timeout 10 --max-time 20 \
        "${_edu_url}/api/subscription/check?code=${EDU_CODE}" 2>/dev/null \
        || echo '{"success":false}')
    # 审计 F-17：用 python 解析 JSON 而非 grep 文本匹配（字段顺序/空白/嵌套变化不再误判）
    if ! echo "${_edu_check}" | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get("is_valid") else 1)' 2>/dev/null; then
        echo -e "${FAIL} Educational deployment code validation failed, please check and retry"; exit 1
    fi
    export EDU_CODE
    echo -e "${OK} Educational deployment code verified"
}

# ── Main entry ──────────────────────────────────────────────────────────

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${FAIL} Please run with sudo: sudo bash ${INSTALL_SCRIPT} [install|update|restart|health|rollback|seed|configure-domain] [--region cn|global] [--skip-deps] [--approve-migrate]"
    exit 1
fi

detect_mode "${1:-}"

# ── Unified entry: resolve deploy type (.env first; interactive only on fresh install) — run for all modes ──
select_deploy_type
apply_deploy_type

# 审计 official-edition guard：VR_EDITION=official 的官方服务器禁止用 install.sh 操作
# （含 curl|bash 一键入口），必须改用 install-official.sh，防止官方版被覆盖为 verorun-pro。
if [ -f "${APP_HOME}/.env" ] && grep -q "^VR_EDITION=official" "${APP_HOME}/.env"; then
    echo -e "${FAIL} This server is the OFFICIAL edition (VR_EDITION=official)."
    echo -e "${FAIL} install.sh cannot be used here — it would re-pull the public verorun-pro."
    echo -e "${FAIL} Use: sudo bash deploy/install-official.sh"
    exit 1
fi

# ── Educational license validation (only in edu + install modes) ──
_edu_license_check

# Audit A-1: install mode approves DB migration and seed by default, ready to use after install (same as install-local.sh)
if [ "${DEPLOY_MODE}" = "install" ]; then
    APPROVE_MIGRATE=1
fi

# Parse flags: --region cn / --region=cn / --skip-deps / --approve-migrate
# Audit H4 fix: the old --region) branch was a no-op, so the value of the space-separated form --region cn was dropped
while [ $# -gt 0 ]; do
    case "${1}" in
        --region=*) REGION="${1#*=}" ;;
        --region) shift; [ $# -gt 0 ] && REGION="${1}" || { echo -e "${FAIL} --region requires a value"; exit 1; } ;;
        --skip-deps) SKIP_DEPS=1 ;;
        --approve-migrate) APPROVE_MIGRATE=1 ;;
        --force) FORCE_UPDATE=1 ;;   # Audit C-3: allow overwriting local changes on update (back up the diff first)
        --admin-user=*) VR_ADMIN_USERNAME="${1#*=}" ;;
        --admin-user) shift; [ $# -gt 0 ] && VR_ADMIN_USERNAME="${1}" || { echo -e "${FAIL} --admin-user requires a value"; exit 1; } ;;
        --admin-pass=*) VR_ADMIN_PASSWORD="${1#*=}" ;;
        --admin-pass) shift; [ $# -gt 0 ] && VR_ADMIN_PASSWORD="${1}" || { echo -e "${FAIL} --admin-pass requires a value"; exit 1; } ;;
        --ssl-email=*) SSL_EMAIL="${1#*=}" ;;
        --ssl-email) shift; [ $# -gt 0 ] && SSL_EMAIL="${1}" || { echo -e "${FAIL} --ssl-email requires a value"; exit 1; } ;;
        *)
            # Audit H-1 fix: detect_domain("${2:-}") only read $2, so when the user passes
            # `install --region cn your-domain.com` in space-separated form, the domain lands in $4,
            # and was previously silently dropped by the while loop. Here, remaining args that are "not a flag, not DEPLOY_MODE,
            # and DOMAIN not yet set" are captured as the domain.
            if [ -z "${DOMAIN}" ] && [[ "${1}" != --* ]] && [ "${1}" != "${DEPLOY_MODE}" ]; then
                DOMAIN="${1}"
                echo -e "${INFO} Domain detected: ${DOMAIN}"
            elif [ -n "${DOMAIN}" ] && [[ "${1}" != --* ]] && [ "${1}" != "${DEPLOY_MODE}" ] && [ "${1}" != "${DOMAIN}" ]; then
                echo -e "${WARN} Domain overridden: ${DOMAIN} → ${1}"
                DOMAIN="${1}"
            fi
            ;;
    esac
    shift
done
# Audit P0-1: resolve the domain once after the while loop finishes (the .env is only a fallback; the CLI domain takes precedence and is not overridden)
detect_domain ""
# Validate region
if [ "${REGION}" != "cn" ] && [ "${REGION}" != "global" ]; then
    echo -e "${FAIL} --region must be 'cn' or 'global' (got: ${REGION})"
    exit 1
fi
echo -e "${INFO} Region: ${REGION}"

# Ask for admin credentials (TTY is still alive)
prompt_admin_creds

case "${DEPLOY_MODE}" in
    install)
        do_install
        ;;
    update)
        do_update
        ;;
    restart)
        restart_services
        ;;
    health)
        health_check
        ;;
    rollback)
        do_rollback
        ;;
    seed)
        do_seed
        ;;
    configure-domain)
        # Audit R1 fix: the while loop has already shifted past $2, so use the global DOMAIN written by detect_domain
        do_configure_domain "${DOMAIN}"
        ;;
    *)
        echo "Usage: sudo bash install.sh [install|update|restart|health|rollback|seed|configure-domain <domain>]"
        exit 1
        ;;
esac
