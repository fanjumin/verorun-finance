#!/bin/bash
# ==========================================================================
# VeroRun — deploy/lib/common.sh
# Shared function library: sourced by deploy/install.sh / deploy/install-code.sh
# via `source`. Defines only functions and idempotent defaults, with no top-level side effects.
# ==========================================================================
# Usage conventions (must be followed by every script):
#   1. Source this file AFTER the script's own default config (GIT_REPO / GIT_BRANCH / APP_HOME / SPARSE_DIRS etc.)
#      is defined, and BEFORE calling any shared function.
#   2. All defaults in this file use the : "${VAR:=...}" form — they do not override values already set by the script.
#   3. Modifying a shared function only requires changing this file in one place; all four scripts pick it up automatically.
#   4. install.sh (domain edition) keeps the B-class functions (generate_env / write_nginx_config /
#      print_summary / do_install / do_update / detect_domain / prompt_domain /
#      do_configure_domain); the three no-domain scripts keep generate_env / write_nginx_config /
#      print_summary / do_install / do_update.
# ==========================================================================

# ── Script name (referenced by the sudoers declaration / prompt text; parameterized to avoid hardcoding) ─────────────
: "${INSTALL_SCRIPT:=$(basename "$0")}"
[ "${INSTALL_SCRIPT}" = "bash" ] && INSTALL_SCRIPT="install.sh"

# ── Idempotent default config (does not override values already set by the script) ─────────────────────────────────
# 审计 C-2：GIT_REPO is defined by each entry script (install.sh / install-code.sh) before sourcing this file —
# common.sh does not define the repo URL, avoiding source confusion.
: "${GIT_BRANCH:=master}"
: "${APP_USER:=${SUDO_USER:-$(whoami)}}"
: "${APP_HOME:=/home/${APP_USER}/verorun}"
: "${VENV_DIR:=${APP_HOME}/venv}"
: "${LOG_DIR:=/var/log/verorun}"
: "${SERVICE_DIR:=/etc/systemd/system}"
: "${REGION:=global}"                # cn | global
# 审计 H-5：Sparse-checkout whitelist (base list). Entry scripts can extend it by appending,
# e.g. install-code.sh runs SPARSE_DIRS="${SPARSE_DIRS} plugins" after sourcing.
# 审计 M-1：appends scripts/ (the README references the scripts/dev_start.py local dev script).
# 审计 H-5 / 根治 2026-08-21：SPARSE_DIRS 采用 "未设置才赋默认、空串保持为空" 语义。
# 原因：官方版/源码版把 SPARSE_DIRS 刻意置空表示"全量检出（disable sparse-checkout）"；
# 若用 ${VAR:=default}，空串会被覆盖回白名单，导致只检出 plugins/site_domains、其余插件丢失。
# 仅当变量从未被赋值（unset）时才填充默认白名单。
# 审计 F-1（2026-08-30 标准版复测）：标准版内置插件（email/sms/im_gateway）必须在稀疏白名单内，
# 否则 5b4dd647 新增的 "Bundled plugin tables" 建表步骤 import 不到模块、全部 skip，
# 管理员后手启用插件时相关功能直接 500。此处与 sync-to-standard.yml 标准版内置插件集合对齐。
# 审计 F-1a（2026-08-30 复测残留）：email/sms/im_gateway 共享依赖 plugins/_base
# （db.py / embeddings.py / ratelimit.py，如 from plugins._base.db import get_raw_connection），
# 白名单必须一并包含 plugins/_base，否则建表/启用仍 No module named 'plugins._base'。
if [ -z "${SPARSE_DIRS+x}" ]; then
    # finance/LAN 桌面包裹版需要 plugins/stock_analysis（白名单制逐项登记）。
    # 仅加入检出清单不激活插件（激活由配置门控），对标准版部署无副作用。
    SPARSE_DIRS="admin auth-center main_site health_service veroguard plugin_manager agent_matrix orchestrator i18n shared providers themes static deploy scripts plugins/site_domains plugins/email plugins/sms plugins/im_gateway plugins/_base plugins/stock_analysis"
fi
: "${FORCE_UPDATE:=0}"              # 审计 C-3：force-overwrite local modifications during update (used with --force)
: "${PIP_MIRROR:=}"
: "${PIP_MIRROR_DETECTED:=}"
: "${VR_ADMIN_CREDS_FILE:=/root/.verorun-creds}"

# ── Colors ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
OK="${GREEN}[OK]${NC}"; WARN="${YELLOW}[WARN]${NC}"; FAIL="${RED}[FAIL]${NC}"; INFO="${BLUE}[i]${NC}"

# 审计 F-3：globally suppress git interactive credential prompts (applies to all git call sites, e.g. update/rollback/configure-domain);
# any credential request fails immediately instead of hanging interactively, preventing infinite stalls when origin points to a mirror.
export GIT_TERMINAL_PROMPT=0

# ══════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════
step() { echo -e "\n${BLUE}═══ $1 ═══${NC}"; }
done_step() { echo -e "${OK} $1"; }
fail_step() { echo -e "${FAIL} $1"; }

# ══════════════════════════════════════════════════════════════════════
# CN Network Auto-Adaptation
# China network environment tuning: apt mirror switching / pip multi-source speed race / git timeout protection
# Fully backward compatible: no switching is triggered in overseas environments (default sources reachable).
# ══════════════════════════════════════════════════════════════════════

# Git operation timeout for clone/fetch. A hard 60s window is too tight on slow CN links and kills
# large private-repo transfers mid-flight. Configurable via GIT_TIMEOUT, default 120s.
# 无人值守加固 ①：the timeout is read at each call site so operators can raise it without editing the script.
: "${GIT_TIMEOUT:=120}"

# 1. apt mirror: if the default source is unreachable within 3s → auto-switch to Aliyun (idempotent, marker-file controlled)
_ensure_apt_mirror() {
    local _marker="/etc/apt/.verorun_mirror_applied"
    [ -f "${_marker}" ] && return 0
    # 审计 F-15：探测实际 apt 元数据文件（Release）而非根路径——GFW 常放行小请求但阻断大流量下载，
    # 根路径可达不代表 apt 下载可用；jammy 为兼容基线（archive.ubuntu.com 保留历史发行版 Release）
    if command -v curl >/dev/null 2>&1 && curl -s --connect-timeout 3 --max-time 8 http://archive.ubuntu.com/ubuntu/dists/jammy/Release -o /dev/null 2>/dev/null; then
        touch "${_marker}"; return 0
    fi
    echo -e "${WARN} Ubuntu default mirror unreachable → switching to Aliyun"
    cp /etc/apt/sources.list "/etc/apt/sources.list.bak.$(date +%s)"
    sed -i 's|http://[^/]*archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list
    sed -i 's|http://[^/]*security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list
    sed -i 's|http://[^/]*ports.ubuntu.com|http://mirrors.aliyun.com|g'   /etc/apt/sources.list
    touch "${_marker}"
    echo -e "${OK} apt mirror → Aliyun"
}

# 2. pip mirror: multi-source speed race (Aliyun → Tsinghua → official) picks the fastest; detected only once
_detect_pip_mirror() {
    [ -n "${PIP_MIRROR_DETECTED:-}" ] && return 0
    echo -e "${INFO} Detecting fastest pip mirror..."
    # G3/DE-4 本地稿：_best_time 阈值 999→3000（国内镜像常 1~2s，旧阈值会"全部超阈值"
    # 而静默回退默认 pypi，导致 CDN 慢源反被选中）；同时记录首个可达镜像作兜底，
    # 全源 >3s 时仍用最快可达镜像而非空回退默认源。
    local _best="" _best_time=3000 _first=""
    for _t in \
        "aliyun|https://mirrors.aliyun.com/pypi/simple/pip/|-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com" \
        "tsinghua|https://pypi.tuna.tsinghua.edu.cn/simple/pip/|-i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn" \
        "pypi|https://pypi.org/simple/pip/|"; do
        local _name="${_t%%|*}"; local _rest="${_t#*|}"; local _url="${_rest%%|*}"; local _args="${_rest#*|}"
        local _start=$(date +%s%N)
        if command -v curl >/dev/null 2>&1 && curl -s --connect-timeout 3 --max-time 5 "${_url}" -o /dev/null 2>/dev/null; then
            local _elapsed=$(( ($(date +%s%N) - _start) / 1000000 ))
            echo -e "  ${INFO} ${_name}: ${_elapsed}ms"
            _first="${_first:-${_args}}"
            if [ "${_elapsed}" -lt "${_best_time}" ]; then
                _best_time="${_elapsed}"; _best="${_args}"
            fi
        else
            echo -e "  ${WARN} ${_name}: unreachable"
        fi
    done
    if [ -n "${_best}" ]; then PIP_MIRROR="${_best}"; elif [ -n "${_first}" ]; then PIP_MIRROR="${_first}"; fi
    PIP_MIRROR_DETECTED=1
    echo -e "${OK} pip mirror → ${PIP_MIRROR:-default}"
}

# 3. git clone timeout protection (60s) + shallow clone acceleration; clear guidance on failure
# Note: fetch --unshallow is not run — sparse-checkout does not depend on full history,
#      and fetching full history re-downloads all data on CN networks, defeating the acceleration purpose.
_clone_with_timeout() {
    local _repo=$1 _dest=$2 _branch=$3
    local _attempt _max="${GIT_CLONE_ATTEMPTS:-3}"
    # Candidate list: direct → ghfast.top → ghproxy.net.
    # The domestic GFW often allows small requests but kills high-traffic transfers: direct probing succeeds
    # yet clone disconnects mid-way (fetch-pack: unexpected disconnect), so a failed clone must
    # automatically fall back to mirrors instead of stubbornly retrying the same URL.
    local _candidates=("${_repo}")
    if echo "${_repo}" | grep -q '^https://github.com/'; then
        _candidates+=("https://ghfast.top/${_repo#https://}" "https://ghproxy.net/${_repo#https://}")
    fi
    # 2026-08-31 gitee 原生源支持：REGION=cn 且源为 fanjumin/verorun-code 时，
    # 优先尝试 gitee 同名 SSH 源（CN 原生直连、免镜像链）。服务器已注册 gitee
    # deploy key 时立即命中；未注册时 publickey 秒级失败，自动落入原候选链，
    # 代价仅数秒。注意 gitee verorun-pro 是落地页仓，不参与该回退。
    if [ "${REGION}" = "cn" ]; then
        case "${_repo}" in
            git@github.com:fanjumin/verorun-code.git|https://github.com/fanjumin/verorun-code.git)
                _candidates=("git@gitee.com:fanjumin/verorun-code.git" "${_candidates[@]}")
                ;;
        esac
    fi
    local _url _cloned=""
    for _url in "${_candidates[@]}"; do
        _attempt=1
        echo -e "${INFO} Cloning ${_url} (timeout ${GIT_TIMEOUT}s, shallow, up to ${_max} attempts)..."
        while [ "${_attempt}" -le "${_max}" ]; do
            # 审计 M-2：--no-single-branch makes the shallow clone (--depth 1) also carry tags, so git describe --tags works for version detection
            if timeout "${GIT_TIMEOUT}" git clone --depth 1 --no-single-branch -b "${_branch}" "${_url}" "${_dest}" 2>&1; then
                _cloned="${_url}"
                break 2
            fi
            echo -e "${WARN} git clone failed (${_url}, attempt ${_attempt}/${_max})"
            # Remove the incomplete clone directory to avoid "already exists" on the next clone
            rm -rf "${_dest}"
            _attempt=$((_attempt + 1))
            [ "${_attempt}" -le "${_max}" ] && sleep $((5 * _attempt))
        done
    done
    if [ -n "${_cloned}" ]; then
        GIT_REPO="${_cloned}"  # record the actually reachable URL for subsequent operations such as update
        return 0
    fi
    echo -e "${FAIL} git clone failed after ${#_candidates[@]} sources x ${_max} attempts (timeout ${GIT_TIMEOUT}s each)"
    echo -e "${INFO} Possible causes:"
    echo -e "${INFO}   1. GitHub unreachable (DNS pollution / GFW)"
    echo -e "${INFO}   2. SSH key not configured (private repo)"
    echo -e "${INFO}   3. Network too slow / mirror flaky"
    echo -e "${INFO} Workarounds:"
    echo -e "${INFO}   • Use a proxy: export https_proxy=... && re-run"
    echo -e "${INFO}   • Pre-clone manually: git clone ${_repo} ${_dest}"
    echo -e "${INFO}   • For public base: use the HTTPS installer from verorun-pro"
    exit 1
}

# --prefer-binary: prefer wheels, fall back to source build
# 审计 C-4：installation failures are reported explicitly + retried up to 3 times (recovers from network jitter / temporary mirror timeouts)
_pip_install() {
    _detect_pip_mirror
    local _attempt=1 _max=3
    while [ "${_attempt}" -le "${_max}" ]; do
        if sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install --timeout 120 --prefer-binary ${PIP_MIRROR} "$@"; then
            return 0
        fi
        echo -e "${WARN} pip install failed (attempt ${_attempt}/${_max}): $*"
        _attempt=$((_attempt + 1))
        [ "${_attempt}" -le "${_max}" ] && sleep 5
    done
    echo -e "${FAIL} pip install failed after ${_max} attempts: $*"
    echo -e "${INFO} Check mirror reachability (${PIP_MIRROR:-default}) or dependency conflicts, then re-run."
    return 1
}

# ══════════════════════════════════════════════════════════════════════
# Python version guarantee (审计 P-1：) — requirements.lock requires Python >= 3.12
# (new dependencies such as numpy 2.4.x have Requires-Python >=3.12). Ubuntu 22.04 defaults
# python3 to 3.10, and creating the venv directly would fail pip resolution due to the version mismatch.
# This function ensures a usable python3.12 exists (built-in on 24.04; 22.04 uses the deadsnakes PPA),
# and writes the full executable path to the global PYTHON_BIN. Idempotent, non-interactive, timeout-protected.
# ══════════════════════════════════════════════════════════════════════
_ensure_python312() {
    export DEBIAN_FRONTEND=noninteractive

    # python3.12 already available → use it directly
    if command -v python3.12 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.12)"
        return 0
    fi

    # system default python3 is already >=3.12 → use it directly
    local _cur=""
    _cur="$(python3 -c 'import sys; print("%d.%d" % (sys.version_info.major, sys.version_info.minor))' 2>/dev/null || true)"
    if [ -n "${_cur}" ]; then
        local _maj="${_cur%%.*}"
        local _min="${_cur#*.}"
        if [ "${_maj}" -gt 3 ] || { [ "${_maj}" -eq 3 ] && [ "${_min}" -ge 12 ]; }; then
            PYTHON_BIN="$(command -v python3)"
            return 0
        fi
    fi

    echo -e "${INFO} System Python ${_cur:-unknown} < 3.12 — installing python3.12 ..."

    # Route 1: shipped with the distro (Ubuntu 24.04 and above)
    if timeout 300 apt-get install -y python3.12 python3.12-venv python3.12-dev 2>&1 && command -v python3.12 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3.12)"
        return 0
    fi

    # Route 2: deadsnakes PPA (Ubuntu 22.04)
    echo -e "${INFO} python3.12 not in distro repos — adding deadsnakes PPA ..."
    timeout 300 apt-get install -y software-properties-common >/dev/null 2>&1 || {
        echo -e "${FAIL} Failed to install software-properties-common (needed for PPA)."
        return 1
    }
    timeout 300 add-apt-repository -y ppa:deadsnakes/ppa 2>&1 || {
        echo -e "${FAIL} Failed to add deadsnakes PPA. Install python3.12 manually, then re-run."
        return 1
    }
    timeout 300 apt-get update 2>&1 || {
        echo -e "${FAIL} apt-get update failed after adding PPA."
        return 1
    }
    timeout 300 apt-get install -y python3.12 python3.12-venv python3.12-dev 2>&1 || {
        echo -e "${FAIL} Failed to install python3.12 from deadsnakes PPA."
        return 1
    }
    command -v python3.12 >/dev/null 2>&1 || {
        echo -e "${FAIL} python3.12 still unavailable after installation."
        return 1
    }
    PYTHON_BIN="$(command -v python3.12)"
    return 0
}

# ══════════════════════════════════════════════════════════════════════
# Virtualenv guarantee (审计 P-1)：automatically rebuilds when the venv is missing or its Python < 3.12.
# The venv contains only dependencies, no business data, so deleting and rebuilding is safe and idempotent.
# Depends on _ensure_python312 to set PYTHON_BIN.
# ══════════════════════════════════════════════════════════════════════
_ensure_venv() {
    if [ -x "${VENV_DIR}/bin/python" ]; then
        local _vver=""
        _vver="$("${VENV_DIR}/bin/python" -c 'import sys; print("%d.%d" % (sys.version_info.major, sys.version_info.minor))' 2>/dev/null || true)"
        if [ -n "${_vver}" ]; then
            local _maj="${_vver%%.*}"
            local _min="${_vver#*.}"
            if [ "${_maj}" -gt 3 ] || { [ "${_maj}" -eq 3 ] && [ "${_min}" -ge 12 ]; }; then
                return 0
            fi
        fi
        echo -e "${WARN} Existing venv Python ${_vver:-unknown} < 3.12 — rebuilding ..."
    else
        echo -e "${INFO} Creating Python venv ..."
    fi

    _ensure_python312 || return 1
    if [ -z "${VENV_DIR}" ] || [ "${VENV_DIR}" = "/" ]; then
        echo -e "${FAIL} Refusing to remove dangerous path: ${VENV_DIR}"
        return 1
    fi
    rm -rf "${VENV_DIR}"
    sudo -u "${APP_USER}" "${PYTHON_BIN}" -m venv "${VENV_DIR}" || {
        echo -e "${FAIL} Failed to create venv with ${PYTHON_BIN}"
        return 1
    }
    return 0
}

# ══════════════════════════════════════════════════════════════════════
# Git SSH auth setup (auto-skipped for HTTPS public repos)
# ══════════════════════════════════════════════════════════════════════
ensure_git_auth() {
    if echo "${GIT_REPO}" | grep -q '^https://'; then
        # 审计 F-1：HTTPS public repos also validate the existing origin, preventing fetch stalls after it is switched to mirrors such as ghfast.top/ghproxy
        if [ -d "${APP_HOME}/.git" ]; then
            local current_url
            current_url=$(git -C "${APP_HOME}" remote get-url origin 2>/dev/null || echo "")
            if [ -n "${current_url}" ] && [ "${current_url}" != "${GIT_REPO}" ]; then
                # 审计 official-edition guard：VR_EDITION=official 的官方服务器禁止被公开入口
                # （install.sh / curl|bash）重新指向公开仓库 verorun-pro，防止官方版被降级覆盖。
                if [ -f "${APP_HOME}/.env" ] && grep -q "^VR_EDITION=official" "${APP_HOME}/.env"; then
                    echo -e "${FAIL} Official edition detected (VR_EDITION=official)."
                    echo -e "${FAIL} Refusing to re-point origin to public repo: ${GIT_REPO}"
                    echo -e "${FAIL} Use: sudo bash deploy/install-official.sh update"
                    exit 1
                fi
                echo -e "${WARN} Git origin mismatch detected — correcting:"
                echo -e "${WARN}   was: ${current_url}"
                echo -e "${WARN}   now: ${GIT_REPO}"
                git -C "${APP_HOME}" remote set-url origin "${GIT_REPO}"
                done_step "Git remote corrected to ${GIT_REPO}"
            fi
        fi
        return 0
    fi
    # 审计 D-8 fix：本地路径仓库（/path/...、./、../、file://）不走 SSH——全新服务器
    # 无 /root/.ssh/id_ed25519 时，若 GIT_REPO 设为本地裸仓库（gitee 无法直连时的离线
    # 中转），旧逻辑会生成 key 并 exit 阻断一键安装。本地仓库走文件系统，无需 key/known_hosts。
    case "${GIT_REPO}" in
        /*|./*|../*|file://*)
            return 0
            ;;
    esac
    local ssh_key="/root/.ssh/id_ed25519"
    # 审计 D-2 fix：按 GIT_REPO 实际 host（github.com / gitee.com / 自定义 SSH host）
    # 动态预热 known_hosts 并给出对应平台的 deploy key 指引，不再写死 github.com
    # （否则 gitee 首连必报 Host key verification failed，私有仓库 SSH 拉取不可用）。
    local _ghost="github.com"
    case "${GIT_REPO}" in
        git@*:*)
            _ghost="${GIT_REPO#git@}"
            _ghost="${_ghost%%:*}"
            ;;
        https://*)
            _ghost="${GIT_REPO#https://}"
            _ghost="${_ghost%%/*}"
            ;;
    esac
    local _keys_url="https://${_ghost}/fanjumin/verorun-code"
    if [ "${_ghost}" = "gitee.com" ]; then
        _keys_url="https://gitee.com/fanjumin/verorun-code/keys"
    elif [ "${_ghost}" = "github.com" ]; then
        _keys_url="https://github.com/fanjumin/verorun-code/settings/keys/new"
    fi
    if [ ! -f "${ssh_key}" ]; then
        echo -e "${INFO} Generating SSH deploy key for git operations..."
        mkdir -p /root/.ssh
        ssh-keygen -t ed25519 -N "" -f "${ssh_key}" -C "verorun-deploy-$(hostname)" >/dev/null 2>&1
        chmod 600 "${ssh_key}"
        chmod 644 "${ssh_key}.pub"
        echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${YELLOW}║  ADD THIS DEPLOY KEY TO YOUR GIT HOST (one-time setup):     ║${NC}"
        echo -e "${YELLOW}╠══════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${YELLOW}║  Host: ${_ghost}${NC}"
        echo -e "${YELLOW}║  URL:  ${_keys_url}${NC}"
        echo -e "${YELLOW}╠══════════════════════════════════════════════════════════════╣${NC}"
        cat "${ssh_key}.pub" | while read -r line; do
            echo -e "${GREEN}║  ${line}${NC}"
        done
        echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"
        echo -e "${WARN} After adding the key, re-run this script to continue."
        exit 0
    fi
    if [ ! -f /root/.ssh/known_hosts ] || ! grep -q "^${_ghost} " /root/.ssh/known_hosts 2>/dev/null; then
        ssh-keyscan "${_ghost}" >> /root/.ssh/known_hosts 2>/dev/null || true
    fi
    if [ -d "${APP_HOME}/.git" ]; then
        local current_url
        current_url=$(git -C "${APP_HOME}" remote get-url origin 2>/dev/null || echo "")
        if echo "${current_url}" | grep -q '^https://'; then
            git -C "${APP_HOME}" remote set-url origin "${GIT_REPO}"
            done_step "Git remote switched to SSH"
        fi
    fi
}

# ══════════════════════════════════════════════════════════════════════
# Git repo URL auto-resolution (审计 Y-1：one-click deployment works across networks without assuming git is pre-installed)
# - HTTPS public repos (install.sh — all non-code types): probe the git smart
#   HTTP endpoint with curl (equivalent to git ls-remote but relies only on curl); when direct GitHub is unreachable,
#   automatically fall back to ghfast.top / ghproxy.net mirrors (the ghproxy.com mirror used by earlier versions is dead;
#   the mirrors here are verified usable; multi-level fallback supported).
# - SSH private repos (install-code.sh): automatically configure SSH over 443
#   (ssh.github.com:443), bypassing the domestically blocked port 22; the repo URL itself is unchanged.
# Called before do_install / do_update pull the code; all four scripts take effect through this shared function.
# ══════════════════════════════════════════════════════════════════════
_probe_git_url() {
    # Probe the git smart HTTP endpoint (the HTTP-side equivalent of git ls-remote, curl only)
    local _url="$1"
    if command -v curl >/dev/null 2>&1; then
        local _code
        _code=$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            "${_url}/info/refs?service=git-upload-pack" 2>/dev/null || echo "000")
        case "${_code}" in
            200|301|302|403) return 0 ;;
        esac
        return 1
    fi
    if command -v git >/dev/null 2>&1; then
        if GIT_TERMINAL_PROMPT=0 timeout 20 git ls-remote "${_url}" >/dev/null 2>&1; then
            return 0
        fi
        return 1
    fi
    # No probing tool (extremely rare): cannot determine, let the following flow try directly
    return 1
}

_setup_ssh_over_443() {
    # SSH port 22 is often blocked domestically: when unreachable, automatically switch to ssh.github.com:443
    local _ssh_conf="/root/.ssh/config"
    if grep -q "Host github.com" "${_ssh_conf}" 2>/dev/null; then
        return 0  # already configured, idempotent
    fi
    if timeout 5 bash -c "exec 3<>/dev/tcp/github.com/22" 2>/dev/null; then
        return 0  # port 22 reachable, no rewrite needed
    fi
    mkdir -p /root/.ssh
    cat >> "${_ssh_conf}" << 'SSHCONF'

# VeroRun auto: SSH over 443 for CN networks (port 22 blocked)
Host github.com
    HostName ssh.github.com
    Port 443
    User git
SSHCONF
    chmod 600 "${_ssh_conf}" 2>/dev/null || true
    echo -e "${WARN} GitHub SSH port 22 unreachable — switched to ssh.github.com:443"
}

_resolve_git_repo() {
    # Only install/update need real access to the remote repo; other modes skip
    case "${DEPLOY_MODE:-}" in
        install|update) ;;
        *) return 0 ;;
    esac

    # SSH private repos (install-code.sh): SSH over 443 to bypass the port-22 block
    if echo "${GIT_REPO}" | grep -q '^git@github.com:'; then
        _setup_ssh_over_443
        return 0
    fi

    # Only github.com HTTPS public repos are handled; custom mirrors/Gitee etc. are used directly without probing
    if ! echo "${GIT_REPO}" | grep -q '^https://github.com/'; then
        return 0
    fi

    local _direct="${GIT_REPO}"
    local _candidates=()
    # Mirrors take priority when REGION=cn (direct is usually slower/unreachable); global prefers direct and falls back on failure
    if [ "${REGION:-global}" = "cn" ]; then
        _candidates=(
            "https://ghfast.top/${_direct#https://}"
            "https://ghproxy.net/${_direct#https://}"
            "${_direct}"
        )
    else
        _candidates=(
            "${_direct}"
            "https://ghfast.top/${_direct#https://}"
            "https://ghproxy.net/${_direct#https://}"
        )
    fi

    local _url
    for _url in "${_candidates[@]}"; do
        if _probe_git_url "${_url}"; then
            if [ "${_url}" != "${_direct}" ]; then
                if [ "${REGION:-global}" = "cn" ]; then
                    echo -e "${INFO} Using git mirror: ${_url}"
                else
                    echo -e "${WARN} GitHub direct unreachable — switching to mirror: ${_url}"
                fi
            fi
            GIT_REPO="${_url}"
            return 0
        fi
    done
    echo -e "${FAIL} Git repo unreachable (tried direct + mirrors)."
    echo -e "${INFO} Fix network, or run with:"
    echo -e "${INFO}   GIT_REPO=<reachable-url> sudo bash ${INSTALL_SCRIPT} ${DEPLOY_MODE}"
    exit 1
}

# ══════════════════════════════════════════════════════════════════════
# Automatic HTTPS certificate issuance (审计 Y-2：enabled only for production + when the domain is configured)
# Flow: install certbot → certbot --nginx issuance (interactive email input) → update .env
# DEPLOY_PROTOCOL=https → reload nginx. Failure does not block installation (Let's Encrypt has
# rate limits; the domain must already resolve to this server). Without a TTY, the script's existing
# interactive fallback applies: skip issuance and print manual commands.
# ══════════════════════════════════════════════════════════════════════
_setup_ssl_cert() {
    if [ "${DEPLOY_TYPE:-}" != "production" ] || [ -z "${DOMAIN:-}" ]; then
        return 0  # only triggered by the domain edition install.sh; the other three scripts naturally skip
    fi

    # 审计 F-06：内网私有CA模式（CERT_SOURCE=private_ca）不走 certbot——
    # 证书由 deploy/intranet/setup_private_ca.sh 预置于 /etc/letsencrypt/live/<domain>/，
    # write_nginx_config 已按该路径自动启用 443；此处仅确认证书存在并置 DEPLOY_PROTOCOL=https。
    if [ "${CERT_SOURCE:-}" = "private_ca" ]; then
        step "HTTPS certificate (private CA)"
        local _ca_cert_dir="/etc/letsencrypt/live/${DOMAIN}"
        local _ip_cert_dir="/etc/verorun/certs/lanip"
        local _ca_ok=0
        if [ -f "${_ca_cert_dir}/fullchain.pem" ] && [ -f "${_ca_cert_dir}/privkey.pem" ]; then
            _ca_ok=1
        # 审计 D-3 fix：IP 场景由 setup_private_ca_ip.sh 签发至 /etc/verorun/certs/lanip/，
        # 同样视为证书就绪，不再误报「SSL skipped」。
        elif echo "${DOMAIN}" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' \
             && [ -f "${_ip_cert_dir}/fullchain.pem" ] && [ -f "${_ip_cert_dir}/privkey.pem" ]; then
            _ca_ok=1
        fi
        if [ "${_ca_ok}" = "1" ]; then
            if grep -q "^DEPLOY_PROTOCOL=" "${APP_HOME}/.env"; then
                sed -i "s/^DEPLOY_PROTOCOL=.*/DEPLOY_PROTOCOL=https/" "${APP_HOME}/.env"
            else
                echo "DEPLOY_PROTOCOL=https" >> "${APP_HOME}/.env"
            fi
            nginx -t && systemctl reload nginx 2>/dev/null || true
            done_step "Private CA certificate detected — DEPLOY_PROTOCOL=https"
        else
            echo -e "${WARN} CERT_SOURCE=private_ca but no certificate at ${_ca_cert_dir} — SSL skipped."
            echo -e "${INFO} Run first: sudo bash deploy/intranet/setup_private_ca.sh ${DOMAIN}"
        fi
        return 0
    fi

    step "HTTPS certificate (Let's Encrypt)"

    local _email="${SSL_EMAIL:-}"

    # --ssl-email flag passed: skip the TTY check and interactive input
    if [ -z "${_email}" ]; then
        if ! { exec 3<>/dev/tty; } 2>/dev/null; then
            exec 3>&-
            echo -e "${WARN} Non-interactive shell — skipping cert issuance."
            echo -e "${INFO} Run later: sudo apt-get install -y certbot python3-certbot-nginx && sudo certbot --nginx -d ${DOMAIN} -d www.${DOMAIN} -d platform.${DOMAIN} -d agent.${DOMAIN}"
            return 0
        fi
        exec 3>&-
        # 审计 P1-3：read with a 30s timeout — a pty environment (CI / ssh -tt) that can open /dev/tty
        # must not hang forever waiting for input; on timeout/skip, cert issuance is deferred with a warning.
        if ! read -r -t 30 -p "  Let's Encrypt email (for renewal notices, optional): " _email < /dev/tty; then
            _email=""
            echo -e "${WARN} No input within 30s — skipping cert issuance (run manually later)."
            echo -e "${INFO} Run later: sudo apt-get install -y certbot python3-certbot-nginx && sudo certbot --nginx -d ${DOMAIN} -d www.${DOMAIN} -d platform.${DOMAIN} -d agent.${DOMAIN}"
            return 0
        fi
    fi

    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get install -y certbot python3-certbot-nginx 2>&1; then
        echo -e "${WARN} certbot install failed — skipping SSL (run manually later)."
        return 0
    fi

    local _cert_args=()
    if [ -z "${_email}" ]; then
        _cert_args=("--register-unsafely-without-email")
    else
        _cert_args=("--agree-tos" "-m" "${_email}")
    fi

    # Issuance failure does not block installation: cert rate limits / domain not resolved / port 80 unreachable etc.
    if certbot --nginx --non-interactive "${_cert_args[@]}" \
        -d "${DOMAIN}" -d "www.${DOMAIN}" -d "platform.${DOMAIN}" -d "agent.${DOMAIN}" \
        --redirect 2>&1; then
        if grep -q "^DEPLOY_PROTOCOL=" "${APP_HOME}/.env"; then
            sed -i "s/^DEPLOY_PROTOCOL=.*/DEPLOY_PROTOCOL=https/" "${APP_HOME}/.env"
        else
            echo "DEPLOY_PROTOCOL=https" >> "${APP_HOME}/.env"
        fi
        nginx -t && systemctl reload nginx 2>/dev/null || true
        done_step "HTTPS certificate issued — DEPLOY_PROTOCOL=https"
    else
        echo -e "${WARN} certbot failed (domain must resolve to this server). SSL skipped — run manually:"
        echo -e "${INFO}   sudo certbot --nginx -d ${DOMAIN} -d www.${DOMAIN} -d platform.${DOMAIN} -d agent.${DOMAIN}"
    fi
}

# ══════════════════════════════════════════════════════════════════════
# Directory conflict handling (docs verorun-deploy-guide.html §6.3: backup/delete/abort)
# ══════════════════════════════════════════════════════════════════════
resolve_directory_conflict() {
    local target_dir="$1"

    # Directory does not exist → normal flow
    if [ ! -d "${target_dir}" ]; then
        return 0
    fi

    # Already a git repo → can update safely
    if [ -d "${target_dir}/.git" ]; then
        echo -e "${OK} Existing VeroRun installation detected at ${target_dir}"
        return 0
    fi

    # Directory exists but is not a git repo → interactive choice
    echo ""
    echo -e "${WARN} ═══════════════════════════════════════════════════════"
    echo -e "${WARN}  Directory conflict detected:"
    echo -e "${WARN}    ${target_dir}"
    echo -e "${WARN}"
    echo -e "${WARN}  This directory exists but is NOT a VeroRun installation."

    # 无人值守加固 ②：AUTO_DIR_CONFLICT (set by -auto / oneshot) resolves the conflict non-interactively.
    # This is the ONLY path that auto-acts on an existing directory; it must be explicitly enabled via a flag.
    # 1 = backup + reinstall (safe default), 2 = delete + reinstall. Always refuse dangerous paths.
    if [ -n "${AUTO_DIR_CONFLICT:-}" ]; then
        case "${AUTO_DIR_CONFLICT}" in
            2)
                if [ -z "${target_dir}" ] || [ "${target_dir}" = "/" ] || [ "${target_dir}" = "${HOME}" ]; then
                    echo -e "${FAIL} Refusing to remove dangerous path: ${target_dir}"
                    exit 1
                fi
                echo -e "${INFO} [auto] Removing ${target_dir} ..."
                rm -rf "${target_dir}"
                echo -e "${OK} [auto] Removed. Proceeding with installation."
                return 0
                ;;
            *)
                local _bak="${target_dir}.bak.$(date +%Y%m%d%H%M%S)"
                echo -e "${INFO} [auto] Backing up to ${_bak} ..."
                mv "${target_dir}" "${_bak}"
                echo -e "${OK} [auto] Backup complete. Proceeding with installation."
                return 0
                ;;
        esac
    fi

    # 审计 M20：no longer auto-deletes without a TTY (curl|sudo bash pipe) — a misjudged directory means irreversible data loss.
    # Instead, abort the installation and require the user to resolve it interactively.
    if ! { exec 3<>/dev/tty; } 2>/dev/null; then
        exec 3>&-
        echo -e "${FAIL} ═══════════════════════════════════════════════════════"
        echo -e "${FAIL}  Non-interactive mode detected. To avoid accidental data loss,"
        echo -e "${FAIL}  installation aborted. Please resolve ${target_dir} manually"
        echo -e "${FAIL}  (move or back it up), then re-run in an interactive terminal."
        echo -e "${FAIL}  Or re-run with -auto to permit non-interactive conflict resolution."
        echo -e "${FAIL} ═══════════════════════════════════════════════════════"
        exit 1
    fi
    exec 3>&-

    echo -e "${WARN}  What would you like to do?"
    echo -e "${WARN}"
    echo -e "${INFO}  [1] Backup and reinstall"
    echo -e "${INFO}      → Move to ${target_dir}.bak.$(date +%Y%m%d%H%M%S) and proceed"
    echo -e "${INFO}  [2] Delete and reinstall"
    echo -e "${INFO}      → Remove ${target_dir} completely and proceed"
    echo -e "${INFO}  [3] Abort installation"
    echo -e "${INFO}      → Exit now. You can manually resolve and re-run."
    echo -e "${WARN} ═══════════════════════════════════════════════════════"

    while true; do
        read -r -p "  Your choice [1/2/3]: " _choice </dev/tty

        case "${_choice}" in
            1)
                local _bak="${target_dir}.bak.$(date +%Y%m%d%H%M%S)"
                echo -e "${INFO} Backing up to ${_bak} ..."
                mv "${target_dir}" "${_bak}"
                echo -e "${OK} Backup complete. Proceeding with installation."
                return 0
                ;;
            2)
                # Safety guard: refuse to delete dangerous paths
                if [ -z "${target_dir}" ] || [ "${target_dir}" = "/" ] || [ "${target_dir}" = "${HOME}" ]; then
                    echo -e "${FAIL} Refusing to remove dangerous path: ${target_dir}"
                    exit 1
                fi
                echo -e "${INFO} Removing ${target_dir} ..."
                rm -rf "${target_dir}"
                echo -e "${OK} Removed. Proceeding with installation."
                return 0
                ;;
            3)
                echo -e "${INFO} Installation aborted by user."
                exit 0
                ;;
            *)
                echo -e "${WARN} Please enter 1, 2, or 3"
                ;;
        esac
    done
}

# ══════════════════════════════════════════════════════════════════════
# DEBUG forced off (production gate: APP_DEBUG=false and FLASK_DEBUG=0 are required before install/update)
# ══════════════════════════════════════════════════════════════════════
assert_debug_disabled() {
    local _dbg
    # Check APP_DEBUG
    _dbg=$(grep -E '^APP_DEBUG=' "${APP_HOME}/.env" 2>/dev/null | tail -1 | cut -d= -f2)
    case "${_dbg}" in
        1|true|TRUE|True|on|yes)
            echo -e "${FAIL} Production install aborted: APP_DEBUG is enabled in .env"
            echo -e "${INFO} Set APP_DEBUG=false in ${APP_HOME}/.env and re-run"
            exit 1 ;;
    esac
    # Check FLASK_DEBUG
    _dbg=$(grep -E '^FLASK_DEBUG=' "${APP_HOME}/.env" 2>/dev/null | tail -1 | cut -d= -f2)
    case "${_dbg}" in
        1|true|TRUE|True|on|yes)
            echo -e "${FAIL} Production install aborted: FLASK_DEBUG is enabled in .env"
            echo -e "${INFO} Set FLASK_DEBUG=0 in ${APP_HOME}/.env and re-run"
            exit 1 ;;
    esac
}

# ══════════════════════════════════════════════════════════════════════
# .env: fill in missing keys (idempotent)
# ══════════════════════════════════════════════════════════════════════
update_env() {
    local env_file="${APP_HOME}/.env"
    if [ ! -f "${env_file}" ]; then
        generate_env
        return
    fi

    local missing=()

    # ── Required keys (randomly generated if missing) ──
    for key in PLUGIN_LICENSE_SECRET CAPTCHA_SECRET_KEY DEV_ACCOUNTS_ENCRYPTION_KEY LICENSE_SERVER_SECRET PROBE_SECRET INTERNAL_SERVICE_TOKEN HEALTH_SECRET; do
        if ! grep -q "^${key}=" "${env_file}" 2>/dev/null; then
            local val
            val=$(python3 -c "import secrets; print(secrets.token_hex(32))")
            echo "${key}=${val}" >> "${env_file}"
            missing+=("${key}")
        fi
    done

    # ── 审计 H-2：required config items (filled with defaults if missing, never overwriting existing values) ──
    # Upgrades from earlier versions may lack DEPLOY_PROTOCOL / APP_REGION / DEPLOY_MARKET etc.;
    # missing them can cause service startup failures or inconsistent runtime behavior, so they are filled in uniformly here.
    local _dom
    # Take the last DEPLOY_DOMAIN line to avoid multi-line variable pollution from historical duplicate lines
    _dom=$(grep "^DEPLOY_DOMAIN=" "${env_file}" 2>/dev/null | tail -1 | cut -d= -f2)
    # 审计 NEW-H3：DEPLOY_PROTOCOL is not auto-inferred (having a domain ≠ having an HTTPS cert configured).
    # Defaults to http when missing; the user adjusts .env according to the actual TLS setup.
    while read -r _k _v; do
        if ! grep -q "^${_k}=" "${env_file}" 2>/dev/null; then
            echo "${_k}=${_v}" >> "${env_file}"
            missing+=("${_k}")
        fi
    done << EOF
DEPLOY_MARKET cn
DEPLOY_TYPE production
DEPLOY_DOMAIN ${_dom}
DEPLOY_PROTOCOL http
DB_PATH ${APP_HOME}/data/x7k2m9a4.db
PG_HOST localhost
PG_PORT 5432
PG_DB appdb
PG_USER app
APP_MODE main
PLUGIN_AUTO_INSTALL 0
APP_REGION ${REGION:-global}
DASHSCOPE_TEXT_KEY 
OPENAI_API_KEY 
DEEPSEEK_API_KEY 
LOG_FILE=${LOG_DIR}/verorun-app.jsonl
EOF

    for key in APP_DEBUG:false FLASK_DEBUG:0; do
        local k="${key%%:*}" v="${key##*:}"
        if ! grep -q "^${k}=" "${env_file}" 2>/dev/null; then
            echo "${k}=${v}" >> "${env_file}"
            missing+=("${k}")
        fi
    done

    # 审计 U-E：DEPLOY_ENV 缺失时按 .env 内 DEPLOY_TYPE 派生补写（未设时应用层默认 dev 会开放 stub-confirm/mock）
    local _cur_dt _de_env
    _cur_dt=$(grep "^DEPLOY_TYPE=" "${env_file}" 2>/dev/null | tail -1 | cut -d= -f2)
    _de_env="dev"
    [ "${_cur_dt}" = "production" ] && _de_env="production"
    if ! grep -q "^DEPLOY_ENV=" "${env_file}" 2>/dev/null; then
        echo "DEPLOY_ENV=${_de_env}" >> "${env_file}"
        missing+=("DEPLOY_ENV")
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        echo -e "${OK} Filled missing keys: ${missing[*]}"
        chmod 600 "${env_file}"
    else
        echo -e "${OK} All keys are present in .env"
    fi
}

# ══════════════════════════════════════════════════════════════════════
# systemd services (four services + the guardian daemon)
# ══════════════════════════════════════════════════════════════════════
# 精简版（edu / minipro）：无声用户控制台服务（verorun-auth / 8083），只有 Admin 身份。
# 其他版本（production / lan / code / dev）完整保留 8083 平台用户控制台。
_skip_user_console() {
    case "${DEPLOY_TYPE:-production}" in
        edu|minipro) return 0 ;;
        *) return 1 ;;
    esac
}

write_systemd_services() {
    local env_file="${APP_HOME}/.env"
    # 审计 H-4 fix：the gunicorn worker count is no longer hardcoded as -w 2.
    # The default stays 2 (backward compatible, no change to existing deployment resource usage);
    # high-concurrency scenarios can override it with the VR_WORKERS env var, e.g.:
    #   VR_WORKERS=4 sudo bash deploy/install.sh update
    # 审计 PERF-001 fix：sync → gthread 线程池，默认 --threads 4（2 workers × 4 threads = 8 并发槽）。
    # pbkdf2 走 OpenSSL 释放 GIL，线程池可并行哈希；可用 VR_THREADS 覆盖，例如 VR_THREADS=8。
    local _workers="${VR_WORKERS:-2}"
    local _threads="${VR_THREADS:-4}"
    write_one_service() {
        local name=$1 port=$2 module=$3 extra_args="${4:-}" runner="${5:-}" runtime_dir="${6:-}"
        local file="${SERVICE_DIR}/${name}.service"
        if [ -n "${runner}" ]; then
            local exec_cmd="${VENV_DIR}/bin/python ${APP_HOME}/${runner} -w ${_workers} -k gthread --threads ${_threads} -b 127.0.0.1:${port} ${extra_args} ${module}:app"
        else
            local exec_cmd="${VENV_DIR}/bin/gunicorn -w ${_workers} -k gthread --threads ${_threads} -b 127.0.0.1:${port} ${extra_args} ${module}:app"
        fi
        local rt_block=""
        if [ -n "${runtime_dir}" ]; then
            rt_block="RuntimeDirectory=${runtime_dir}
RuntimeDirectoryMode=0755"
        fi
        cat > "${file}" << SVCEOF
[Unit]
Description=VeroRun ${name}
After=network.target postgresql.service

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_HOME}
EnvironmentFile=${env_file}
ExecStart=${exec_cmd}
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
TimeoutStartSec=300
# Startup health check: /health must return 200, otherwise systemd marks startup failed
ExecStartPost=${APP_HOME}/deploy/health_check.sh ${port}
StandardOutput=append:${LOG_DIR}/${name}.log
StandardError=append:${LOG_DIR}/${name}.log
${rt_block}

[Install]
WantedBy=multi-user.target
SVCEOF
        systemctl daemon-reload
        systemctl enable "${name}"
    }
    # 8081 — Main site (homepage public_home.html)
    write_one_service "verorun-main" 8081 "auth_server" "--timeout 120 --log-level warning"
    # 8083 — Platform / User Console (skipped in 精简版 edu/minipro, only Admin identity)
    if _skip_user_console; then
        echo -e "${INFO} 精简版 ${DEPLOY_TYPE:-production}: skipping verorun-auth (8083, user console)"
    else
        write_one_service "verorun-auth" 8083 "main_site" "--timeout 120 --log-level warning"
    fi
    # 8084 — Admin (uses run_gunicorn.py to avoid platform/ shadowing stdlib)
    # RuntimeDirectory=verorun → systemd creates /run/verorun/ owned by APP_USER on service start.
    write_one_service "verorun-admin" 8084 "admin.app" "--timeout 300 --max-requests=1000 --graceful-timeout=30 --log-level warning --config admin/gunicorn_config.py" "admin/run_gunicorn.py" "verorun"
    # 8085 — Health Check
    write_one_service "verorun-health" 8085 "health_service.app" "--timeout 30 --graceful-timeout=30 --log-level warning"
    # ── verorun-guardian (standalone daemon, no HTTP port) ──
    write_guardian_service
    write_guardian_env
}

write_guardian_service() {
    local file="${SERVICE_DIR}/verorun-guardian.service"
    # 精简版（edu/minipro）无 verorun-auth(8083)，Wants 不含该服务
    local _gwants="verorun-auth.service"
    if _skip_user_console; then
        _gwants="#verorun-auth.service"
    fi
    cat > "${file}" << 'GDEVEOF'
[Unit]
Description=VeroGuard — Unified Guardian Daemon (Health + Integrity + Heartbeat)
After=network.target postgresql.service
Wants=verorun-health.service verorun-main.service verorun-admin.service GDPLATFORM

[Service]
Type=simple
# Guardian runs as root to access system integrity checks (file hashes,
# process monitoring, and systemd journal) that require elevated privileges.
# Task 4（二进制化）: VR_EDITION=official（官方版）直接跑 Python 源码；
# 非官方版必须运行 Nuitka 编译产物 verorun-guardian.bin，
# 二进制缺失/被删除 → fail-closed，守护进程拒绝启动。
User=root
WorkingDirectory=GDEVDIR
EnvironmentFile=-/etc/default/verorun-guardian
ExecStart=/bin/sh -c 'if [ "${VR_EDITION}" = "official" ]; then exec GDEVDIR/venv/bin/python GDEVDIR/veroguard/guardian.py; elif [ -x GDEVDIR/veroguard/dist/verorun-guardian.bin ]; then exec GDEVDIR/veroguard/dist/verorun-guardian.bin; else echo "VeroGuard binary missing (non-official) — fail-closed" >&2; exit 1; fi'
Restart=always
RestartSec=5
RuntimeDirectory=verorun-guardian
RuntimeDirectoryMode=0755
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
GDEVEOF
    sed -i "s|GDPLATFORM|${_gwants}|g" "${file}"
    sed -i "s|GDEVDIR|${APP_HOME}|g" "${file}"
    systemctl daemon-reload
    # STD-4 修复（2026-08-29 标准版部署测试）：非官方版 Guardian 依赖 Nuitka 产物
    # veroguard/dist/verorun-guardian.bin，而仓库 .gitignore 排除 dist/ → 标准版安装后
    # 二进制必然缺失，ExecStart fail-closed → systemd Restart=always 无限崩溃循环
    # （实测 4 分钟 52 次 restart）。修复：二进制缺失且非官方版 → 写入 unit 但不 enable，
    # 给出可执行修复指引，让一键安装得到可用系统（无守护）而非 crash-loop。
    local _vr_ed="standard"
    if [ -f "${APP_HOME}/.env" ]; then
        _vr_ed=$(grep "^VR_EDITION=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2- || true)
    fi
    _vr_ed="${_vr_ed:-standard}"
    if [ "${_vr_ed}" != "official" ] && [ ! -x "${APP_HOME}/veroguard/dist/verorun-guardian.bin" ]; then
        echo -e "${WARN} VeroGuard binary missing (non-official edition) — verorun-guardian NOT enabled (fail-closed guard avoided)."
        echo -e "${INFO}   To enable integrity guarding: ship veroguard/dist/verorun-guardian.bin (Nuitka build) in the release,"
        echo -e "${INFO}   then run: sudo systemctl enable --now verorun-guardian"
        # STD-4 补强：存量系统（此前已被 enable）在 update 时顺带 disable，避免崩溃循环延续
        systemctl disable verorun-guardian 2>/dev/null || true
        systemctl stop verorun-guardian 2>/dev/null || true
        return 0
    fi
    systemctl enable verorun-guardian
}

write_guardian_env() {
    local env_file="/etc/default/verorun-guardian"
    local probe_secret=""
    local guardian_remote=""
    local deployment_code=""
    local vr_edition=""
    if [ -f "${APP_HOME}/.env" ]; then
        probe_secret=$(grep "^PROBE_SECRET=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2) || true
        [ -n "${probe_secret}" ] && probe_secret="PROBE_SECRET=${probe_secret}"
        vr_edition=$(grep "^VR_EDITION=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2-) || true
        # 审计 M21：GUARDIAN_REMOTE_URL / DEPLOYMENT_CODE can be overridden in .env,
        # no longer hardcoding a single global address (CN-region deployments explicitly configure the regional API address in .env)
        guardian_remote=$(grep "^GUARDIAN_REMOTE_URL=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2-) || true
        deployment_code=$(grep "^DEPLOYMENT_CODE=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2-) || true
    fi
    if [ -z "${guardian_remote}" ]; then
        # 审计 M21：fall back to the global default address when not configured in .env
        guardian_remote="https://api.verorun.com"
    fi
    [ -z "${vr_edition}" ] && vr_edition="${VR_EDITION:-standard}"
    cat > "${env_file}" << GENVEOF
# VeroGuard Guardian environment config — generated by ${INSTALL_SCRIPT}
GUARDIAN_PROJECT_DIR=${APP_HOME}
GUARDIAN_LOG_FILE=${LOG_DIR}/verorun-guardian.log
GUARDIAN_CHECK_INTERVAL=30
GUARDIAN_MAX_FAILURES=3
GUARDIAN_COOLDOWN=300
GUARDIAN_INTEGRITY_INTERVAL=300
GUARDIAN_HEARTBEAT_INTERVAL=300
GUARDIAN_WEBHOOK_URL=
GUARDIAN_REMOTE_URL=${guardian_remote}
${probe_secret}
DEPLOYMENT_CODE=${deployment_code}
VR_EDITION=${vr_edition}
GENVEOF
    chmod 600 "${env_file}"
}

# ══════════════════════════════════════════════════════════════════════
# sudoers — one-click update permissions (declarative, idempotent)
# ══════════════════════════════════════════════════════════════════════
write_sudoers() {
    local sudoers_file="/etc/sudoers.d/verorun"
    # 精简版（edu/minipro）无 verorun-auth(8083)，不授予其 restart 权限
    local _auth_sudoers=""
    if ! _skip_user_console; then
        _auth_sudoers="${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart verorun-auth"
    fi
    cat > "${sudoers_file}" << SUEOF
# Managed by VeroRun ${INSTALL_SCRIPT} — regenerated on every install/update
# Grants ${APP_USER} passwordless one-click update for VeroRun services
${APP_USER} ALL=(root) NOPASSWD: /bin/bash ${APP_HOME}/deploy/${INSTALL_SCRIPT} update
${APP_USER} ALL=(root) NOPASSWD: /bin/bash ${APP_HOME}/deploy/${INSTALL_SCRIPT} restart
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart verorun-main
${_auth_sudoers}
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart verorun-admin
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart verorun-health
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart verorun-guardian
${APP_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx
${APP_USER} ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload  # 审计 M2：applies the auth-center dynamic subdomain configuration
SUEOF
    chmod 440 "${sudoers_file}"
    visudo -c -f "${sudoers_file}" || {
        echo -e "${FAIL} Invalid sudoers file — restoring previous state"
        rm -f "${sudoers_file}"
        exit 1
    }
}

# ══════════════════════════════════════════════════════════════════════
# Service restart (with startup-wait polling + nginx)
# ══════════════════════════════════════════════════════════════════════
restart_services() {
    local services=("verorun-admin" "verorun-main" "verorun-health" "verorun-guardian")
    if ! _skip_user_console; then
        services+=("verorun-auth")
    fi
    for svc in "${services[@]}"; do
        if systemctl is-enabled --quiet "${svc}" 2>/dev/null; then
            # 审计 2026-08-15：首次启动可能因空库并发建表竞态失败（如 admin 双 worker），
            # 不得在 set -e 下中断安装流程 —— 记录 WARN 后交由下方 wait 轮询 + Restart=always 兜底恢复。
            if ! systemctl restart "${svc}" 2>/dev/null; then
                echo -e "${WARN} ${svc} restart returned non-zero — waiting for recovery below"
            fi
            local _waited=0
            while [ $_waited -lt 60 ]; do
                if systemctl is-active --quiet "${svc}" 2>/dev/null; then
                    echo -e "${OK} ${svc} is running"
                    break
                fi
                sleep 2
                _waited=$((_waited + 2))
            done
            if [ $_waited -ge 60 ]; then
                echo -e "${FAIL} ${svc} failed to start — check: journalctl -u ${svc} -n 20"
            fi
        else
            echo -e "${WARN} ${svc} not configured, skipping"
        fi
    done
    if systemctl is-enabled --quiet nginx 2>/dev/null; then
        # 审计 2026-08-15：与上方服务重启同源加固 —— nginx 重启失败不中断安装，输出 WARN 供排查。
        if ! systemctl restart nginx 2>/dev/null; then
            echo -e "${WARN} nginx restart returned non-zero — check: nginx -t"
        else
            echo -e "${OK} nginx restarted"
        fi
    fi
}

# ══════════════════════════════════════════════════════════════════════
# Dependency scan
# ══════════════════════════════════════════════════════════════════════
check_system_deps() {
    local pkg
    for pkg in python3 python3-venv python3-pip python3-dev nginx git curl wget \
        build-essential libpq-dev libssl-dev postgresql postgresql-client; do
        if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
            return 1
        fi
    done
    return 0
}

check_python_deps() {
    [ -x "${VENV_DIR}/bin/python" ] || return 1
    [ -f "${APP_HOME}/requirements.txt" ] || return 1
    local freeze line pkg
    freeze=$("${VENV_DIR}/bin/pip" list --format=freeze 2>/dev/null) || return 1
    while read -r line; do
        [ -z "${line}" ] && continue
        # 审计 H-6：skip comment / pip option / -e editable install / git+ / http(s) / file: etc. non-regular package lines
        case "${line}" in
            \#*|--*|-e*|git+*|http://*|https://*|file:*|[-!+]*|.) continue ;;
        esac
        pkg="${line%%[<>=!~;@]*}"
        pkg="${pkg%%\[*}"   # strip extras (e.g. flask[async])
        pkg=$(printf '%s' "${pkg}" | tr 'A-Z' 'a-z' | tr '_' '-')
        # Exact-match installed package names (^ anchors the line start, avoiding prefix mismatches such as discord.py matching discord)
        printf '%s\n' "${freeze}" | grep -qi "^${pkg}==\|^${pkg} @ " || return 1
    done < "${APP_HOME}/requirements.txt"
    return 0
}

# ══════════════════════════════════════════════════════════════════════
# Mode detection
# ══════════════════════════════════════════════════════════════════════
detect_mode() {
    local mode="${1:-}"
    if [ -n "$mode" ]; then
        DEPLOY_MODE="$mode"
    elif [ -f "${APP_HOME}/.env" ]; then
        DEPLOY_MODE="update"
    else
        DEPLOY_MODE="install"
    fi
    echo -e "${INFO} Deploy mode: ${DEPLOY_MODE}"
}

# ══════════════════════════════════════════════════════════════════════
# Interactive admin credential creation (called in install mode when a TTY is available)
# ══════════════════════════════════════════════════════════════════════
prompt_admin_creds() {
    case "${DEPLOY_MODE}" in install) ;; *) return 0 ;; esac
    [ -f "${VR_ADMIN_CREDS_FILE}" ] && return 0

    # --admin-user / --admin-pass flags: both must be provided together.
    # 审计 F3 修复：只传其一 → 显式失败退出，而非静默落入自动生成覆盖用户输入。
    if [ -n "${VR_ADMIN_USERNAME:-}" ] || [ -n "${VR_ADMIN_PASSWORD:-}" ]; then
        if [ -z "${VR_ADMIN_USERNAME:-}" ] || [ -z "${VR_ADMIN_PASSWORD:-}" ]; then
            echo -e "${FAIL} --admin-user and --admin-pass must be provided together"
            exit 1
        fi
        printf 'VR_ADMIN_USERNAME="%s"\nVR_ADMIN_PASSWORD="%s"\n' "${VR_ADMIN_USERNAME}" "${VR_ADMIN_PASSWORD}" > "${VR_ADMIN_CREDS_FILE}"
        chmod 600 "${VR_ADMIN_CREDS_FILE}"
        trap 'rm -f "${VR_ADMIN_CREDS_FILE}"' EXIT
        echo -e "${OK} Admin credentials set via flag"
        return 0
    fi

    # Non-interactive pipe (curl | sudo bash without TTY): auto-fallback, credentials generated by seed_data.py
    if ! { exec 3<>/dev/tty; } 2>/dev/null; then
        echo -e "${INFO} Non-interactive shell — admin credentials auto-generated"
        return 0
    fi
    exec 3>&-

    echo "" > /dev/tty
    echo -e "${INFO} Create the administrator account for VeroRun" > /dev/tty

    local _user="" _pass="" _pass2=""
    echo -n "  Admin username: " > /dev/tty
    read -r _user < /dev/tty
    _user="${_user//[^a-zA-Z0-9._-]/}"
    while [ -z "${_user}" ]; do
        echo -e "${WARN} Username cannot be empty" > /dev/tty
        echo -n "  Admin username: " > /dev/tty
    read -r _user < /dev/tty
    done

    echo -n "  Admin password: " > /dev/tty
    read -r -s _pass < /dev/tty
    echo "" > /dev/tty
    while [ -z "${_pass}" ]; do
        echo -e "${WARN} Password cannot be empty" > /dev/tty
        echo -n "  Admin password: " > /dev/tty
    read -r -s _pass < /dev/tty
        echo "" > /dev/tty
    done

    echo -n "  Confirm password: " > /dev/tty
    read -r -s _pass2 < /dev/tty
    echo "" > /dev/tty
    while [ "${_pass}" != "${_pass2}" ]; do
        echo -e "${WARN} Passwords do not match, try again" > /dev/tty
        echo -n "  Admin password: " > /dev/tty
    read -r -s _pass < /dev/tty
        echo "" > /dev/tty
        echo -n "  Confirm password: " > /dev/tty
    read -r -s _pass2 < /dev/tty
        echo "" > /dev/tty
    done

    printf 'VR_ADMIN_USERNAME="%s"\nVR_ADMIN_PASSWORD="%s"\n' "${_user}" "${_pass}" > "${VR_ADMIN_CREDS_FILE}"
    chmod 600 "${VR_ADMIN_CREDS_FILE}"
    # 审计 C2 hardening：cleans up the credentials file when the script exits abnormally (prompt is only called in install mode, no EXIT trap conflict)
    trap 'rm -f "${VR_ADMIN_CREDS_FILE}"' EXIT
    echo -e "${OK} Admin credentials saved"
}

# ══════════════════════════════════════════════════════════════════════
# Health check
# ══════════════════════════════════════════════════════════════════════
health_check() {
    echo ""
    local all_ok=true

    check_port() {
        local port=$1 name=$2
        local code
        # 审计 H-7：--max-time 10 prevents curl from hanging when a service accepts connections but never responds, which would stall the health check
        code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "http://127.0.0.1:${port}/" 2>/dev/null || echo "000")
        if [ "$code" != "000" ]; then
            echo -e "  ${OK} ${name} (:${port}) -> HTTP ${code}"
        else
            echo -e "  ${FAIL} ${name} (:${port}) -> no response"
            all_ok=false
        fi
    }

    check_port 8081 "verorun-main"
    if ! _skip_user_console; then
        check_port 8083 "verorun-auth"
    fi
    check_port 8084 "verorun-admin"
    check_port 8085 "verorun-health"

    if systemctl is-active --quiet verorun-guardian 2>/dev/null; then
        echo -e "  ${OK} verorun-guardian (systemd)"
    else
        echo -e "  ${FAIL} verorun-guardian (inactive)"
        all_ok=false
    fi

    echo ""
    echo -e "${INFO} Migration log check:"
    local _mig_services=("verorun-admin" "verorun-main")
    if ! _skip_user_console; then
        _mig_services+=("verorun-auth")
    fi
    for svc in "${_mig_services[@]}"; do
        journalctl -u "${svc}" --since "1 min ago" 2>/dev/null | grep -i "\[Migration\]" | tail -2 || true
    done

    if $all_ok; then
        echo -e "\n${OK} All services healthy"
    else
        echo -e "\n${FAIL} Some services are unhealthy — check logs"
        UPDATE_FAILED=1
    fi
}

# ══════════════════════════════════════════════════════════════════════
# VeroGuard integrity manifest build (审计 NEW-H1：called uniformly by all four deployment modes)
# ══════════════════════════════════════════════════════════════════════
build_veroguard_manifest() {
    # Task 5（签名清单）: 完整性清单由发版流程生成并随代码分发（manifest.json + manifest.json.sig），
    # 服务器端禁止本地重生成（无 RELEASE_SIGN_KEY 无法生成有效签名清单）。
    step "Verify integrity manifest (VeroGuard)"
    local _mf="${APP_HOME}/veroguard/data/manifest.json"
    if [ ! -f "${_mf}" ] || [ ! -f "${_mf}.sig" ]; then
        echo -e "${FAIL} Integrity manifest (manifest.json/.sig) missing — aborting"
        echo -e "${INFO} 完整性清单随发版分发，缺失说明代码包不完整或未按发版流程生成签名清单"
        exit 1
    fi
    # semver 一致性：manifest 必须与当前 VERSION 匹配，防止清单与代码版本错位导致误锁
    local _mf_semver _cur_semver
    _mf_semver=$("${VENV_DIR}/bin/python" -c "import json;print(json.load(open('${_mf}',encoding='utf-8')).get('semver',''))" 2>/dev/null) || true
    _cur_semver=$(cat "${APP_HOME}/VERSION" 2>/dev/null | tr -d '[:space:]') || true
    if [ -n "${_mf_semver}" ] && [ -n "${_cur_semver}" ] && [ "${_mf_semver}" != "${_cur_semver}" ]; then
        echo -e "${FAIL} Manifest semver ${_mf_semver} != VERSION ${_cur_semver} — aborting（清单与代码版本不匹配）"
        exit 1
    fi
    # Ed25519 验签（fail-closed：验签失败拒绝继续）
    if ! "${VENV_DIR}/bin/python" "${APP_HOME}/veroguard/tools/build_manifest.py" --verify --project-dir "${APP_HOME}" 2>&1; then
        echo -e "${FAIL} Integrity manifest signature invalid — aborting"
        exit 1
    fi
    done_step "Integrity manifest verified"
}

verify_release_manifest() {
    # Task 5（安装期验签）: 非官方环境必须携带已签名发布清单 release_manifest.json/.sig，
    # Ed25519 验签失败 → 拒绝安装/更新（fail-closed）。官方版信任自身，跳过门禁。
    local _edition
    _edition=$(grep "^VR_EDITION=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2-) || true
    if [ "${_edition}" = "official" ]; then
        echo -e "${INFO} VR_EDITION=official — 跳过发布清单验签门禁"
        return 0
    fi
    if [ ! -f "${APP_HOME}/release_manifest.json" ] || [ ! -f "${APP_HOME}/release_manifest.sig" ]; then
        echo -e "${FAIL} release_manifest.json/.sig 缺失 — 非官方环境必须携带已签名发布清单，拒绝继续"
        exit 1
    fi
    if ! "${VENV_DIR}/bin/python" "${APP_HOME}/deploy/scripts/sign_release.py" --check 2>&1; then
        echo -e "${FAIL} release_manifest 验签失败 — 拒绝安装/更新"
        echo -e "${INFO} 常见原因：VERSION 已 bump 但发版清单未用 RELEASE_SIGN_KEY 重签（semver 错位），"
        echo -e "${INFO}   或 deploy 脚本/common.sh 在签名后被修改（制品哈希不符）。"
        echo -e "${INFO} 维护者修复：python3 deploy/scripts/sign_release.py（需 RELEASE_SIGN_KEY）→ 提交推送 → 重新安装。"
        exit 1
    fi
    echo -e "${OK} release_manifest 验签通过"
}

# ══════════════════════════════════════════════════════════════════════
# Seed data
# ══════════════════════════════════════════════════════════════════════
do_seed() {
    step "Seed initial data"
    if [ ! -f "${VENV_DIR}/bin/python" ]; then
        echo -e "${FAIL} Python venv not found at ${VENV_DIR}"
        echo -e "${INFO} Run '${INSTALL_SCRIPT}' first"
        exit 1
    fi

    # Seed mode explicitly requested via command-line → always execute (overrides --approve-migrate gate)
    if [ "${DEPLOY_MODE}" = "seed" ]; then
        APPROVE_MIGRATE=1
    fi

    # Seed is grouped under the same manual gate as DB migration
    if [ "${APPROVE_MIGRATE:-0}" != "1" ]; then
        echo -e "${WARN} Skipped seed data — admin account NOT created, admin panel inaccessible"
        echo -e "${WARN} To create admin account now, run: sudo bash deploy/install.sh seed"
        return 0
    fi

    # Read credentials from temp file (set by prompt_admin_creds)
    VR_ADMIN_USERNAME=""
    VR_ADMIN_PASSWORD=""
    if [ -f "${VR_ADMIN_CREDS_FILE}" ]; then
        # shellcheck disable=SC1090
        source "${VR_ADMIN_CREDS_FILE}"
        rm -f "${VR_ADMIN_CREDS_FILE}"
    fi

    # 审计 NEW-M1：credentials are finalized here, ensuring print_summary shows the same password that seed_data.py actually writes.
    # With no credentials, generate a default admin (administrator + random password) and always pass it to seed_data.py via environment variables.
    local _auto_admin=0
    if [ -z "${VR_ADMIN_USERNAME}" ]; then
        VR_ADMIN_USERNAME="administrator"
    fi
    if [ -z "${VR_ADMIN_PASSWORD}" ]; then
        VR_ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(8))")
        _auto_admin=1
    fi
    # 审计 S2：非交互安装自动生成的 admin 密码必须在安装输出中显式打印一次，
    # 否则 curl|bash 安装后无人能登录 admin（print_summary 只显示 ***HIDDEN***）。
    # 审计 F-05：同时持久化到 ${APP_HOME}/.verorun-admin-credentials（chmod 600 / chown APP_USER），
    # 避免"仅打印一次、终端滚动后永久丢失"；明文仅存在于该文件与安装终端输出。
    if [ "${_auto_admin}" = "1" ]; then
        echo -e "${WARN} ──────────────────────────────────────────────"
        echo -e "${WARN} Auto-generated admin credentials — SAVE NOW:"
        echo -e "${OK}   Admin username: ${VR_ADMIN_USERNAME}"
        echo -e "${OK}   Admin password: ${VR_ADMIN_PASSWORD}"
        echo -e "${WARN} ──────────────────────────────────────────────"
        local _cred_file="${APP_HOME}/.verorun-admin-credentials"
        umask 077
        cat > "${_cred_file}" <<EOF
VR_ADMIN_USERNAME=${VR_ADMIN_USERNAME}
VR_ADMIN_PASSWORD=${VR_ADMIN_PASSWORD}
EOF
        chmod 600 "${_cred_file}"
        chown "${APP_USER}:${APP_USER}" "${_cred_file}" 2>/dev/null || true
        echo -e "${INFO} Credentials also saved (chmod 600): ${_cred_file}"
    fi

    # 审计 C1：admin credentials are passed to seed_data.py via environment variables, avoiding exposure in the process command line
    # 回归修复：seed_data.py auto-detects `.env` from the *current working directory*; when install runs as root,
    # that resolves to /root/.env (or an unrelated home .env) instead of ${APP_HOME}/.env, causing
    # "Tables not found" even though migration succeeded. Pin the env path explicitly via its supported --env flag.
    sudo -u "${APP_USER}" env VR_ADMIN_USERNAME="${VR_ADMIN_USERNAME}" VR_ADMIN_PASSWORD="${VR_ADMIN_PASSWORD}" \
        "${VENV_DIR}/bin/python" "${APP_HOME}/deploy/seed_data.py" --env "${APP_HOME}/.env"
    echo -e "${OK} Seed data injected"
}

# ══════════════════════════════════════════════════════════════════════
# Rollback (uniformly uses the before_commit save point; falls back to HEAD~1 when missing)
# ══════════════════════════════════════════════════════════════════════
do_rollback() {
    step "Rollback to previous version"
    cd "${APP_HOME}"
    git reflog --oneline -5 | head -5
    local target_commit
    if [ -f "${APP_HOME}/.rollback/before_commit" ]; then
        target_commit=$(head -1 "${APP_HOME}/.rollback/before_commit" | awk '{print $1}')
        echo -e "${INFO} Rolling back to saved commit: ${target_commit}"
    else
        target_commit="HEAD~1"
        echo -e "${WARN} No saved commit found, falling back to HEAD~1"
    fi
    # 审计 F-14：reset --hard 前保存当前状态到保护分支，回滚后可安全找回原提交
    local _rollback_branch="rollback-$(date +%s)"
    if git branch "${_rollback_branch}" 2>/dev/null; then
        echo -e "${INFO} Current state saved to branch: ${_rollback_branch}"
    else
        echo -e "${WARN} Failed to create safety branch — proceeding anyway"
    fi
    if git reset --hard "${target_commit}"; then
        if _skip_user_console; then
            systemctl restart verorun-admin verorun-main verorun-health verorun-guardian
        else
            systemctl restart verorun-admin verorun-auth verorun-main verorun-health verorun-guardian
        fi
        echo -e "${OK} Rolled back to $(git log --oneline -1)"
    else
        echo -e "${FAIL} Rollback failed"
    fi
}

# ══════════════════════════════════════════════════════════════════════
# C-1 unified deployment functions (审计 R4 enabled: shared by the entry scripts, driven by DEPLOY_TYPE)
# DEPLOY_TYPE: production | lan | code | edu
# Defined by each entry script before sourcing this file (install.sh=production/lan/edu, install-code=code).
# The entry scripts no longer define same-named functions themselves,
# so Bash no longer has "later definitions overriding earlier ones"; the unified versions here take effect directly.
# ══════════════════════════════════════════════════════════════════════

# ── .env generation: header comment / DEPLOY_DOMAIN / DEPLOY_PROTOCOL driven by DEPLOY_TYPE ──
generate_env() {
    local env_file="${APP_HOME}/.env"
    local force="${1:-}"

    if [ -f "${env_file}" ] && [ "${force}" != "force" ]; then
        echo -e "${WARN} .env already exists, skipping"
        return
    fi

    if [ -f "${env_file}" ] && [ "${force}" = "force" ]; then
        cp "${env_file}" "${env_file}.bak.$(date +%s)" 2>/dev/null || true
        if [ "${DEPLOY_TYPE}" = "production" ]; then
            echo -e "${INFO} Existing .env backed up"
        fi
    fi

    if [ -z "${PG_PASSWORD:-}" ]; then
        PG_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(16))")
    fi
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    FLASK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    ENCRYPTION_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    PLUGIN_LICENSE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    CAPTCHA_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    DEV_ACCOUNTS_ENCRYPTION_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    LICENSE_SERVER_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    PROBE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    INTERNAL_SERVICE_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    # 审计 D5：HEALTH_SECRET is randomly generated to avoid the publicly hardcoded default from the source
    HEALTH_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # ── DEPLOY_TYPE driven: header comment / DOMAIN / PROTOCOL / ENV ──
    local _env_header="VeroRun config — auto-generated by ${INSTALL_SCRIPT} (no-domain / LAN mode)"
    local _deploy_domain=""
    local _deploy_protocol="http"
    local _deploy_env="dev"
    case "${DEPLOY_TYPE}" in
        production)
            _env_header="VeroRun production config — auto-generated by ${INSTALL_SCRIPT}"
            _deploy_domain="${DOMAIN:-}"
            # 审计 F-11：仅在有域名时默认 https；无域名（纯 HTTP 运行）保持 http，
            # 否则 DEPLOY_PROTOCOL=https 会让应用层 JWT secure cookie 在 HTTP 下登录失效。
            [ -n "${DOMAIN:-}" ] && _deploy_protocol="https"
            # 审计 U-E：production 显式标记 DEPLOY_ENV=production（未设时应用层默认 dev 会开放 stub-confirm/mock）
            _deploy_env="production"
            ;;
        code)
            _env_header="VeroRun config — auto-generated by ${INSTALL_SCRIPT} (no-domain / LAN mode, full plugins)"
            ;;
        edu)
            _env_header="VeroRun Educational config — auto-generated by ${INSTALL_SCRIPT} (no-domain, edu license)"
            ;;
    esac

    cat > "${env_file}" << ENVEOF
# ${_env_header}
DEPLOY_MARKET=cn
DEPLOY_TYPE=${DEPLOY_TYPE}
DEPLOY_ENV=${_deploy_env}
DEPLOY_DOMAIN=${_deploy_domain}
DEPLOY_PROTOCOL=${_deploy_protocol}
DB_PATH=${APP_HOME}/data/x7k2m9a4.db
PG_HOST=localhost
PG_PORT=5432
PG_DB=appdb
PG_USER=app
PG_PASSWORD=${PG_PASSWORD}
JWT_SECRET=${JWT_SECRET}
FLASK_SECRET_KEY=${FLASK_SECRET}
ENCRYPTION_KEY=${ENCRYPTION_KEY}
APP_MODE=main

# 审计 M2：output directory for L2 subdomain nginx snippets (auth-center dynamic subdomain config written to disk, included by nginx by default)
NGINX_SNIPPETS_DIR=/etc/nginx/sites-enabled

# 审计 M7：Basic Auth for the admin panel at the nginx layer (defense in depth, disabled by default).
# To enable: change to 1 + set the username, then run
#   sudo htpasswd -c /etc/nginx/.verorun-admin ${ADMIN_BASIC_AUTH_USER:-admin}
ADMIN_BASIC_AUTH=0
ADMIN_BASIC_AUTH_USER=admin

# Internal service token
INTERNAL_SERVICE_TOKEN=${INTERNAL_SERVICE_TOKEN}

# Internal service API base URL (main_site runs on verorun-auth / 8083)
MAIN_SITE_INTERNAL_URL=http://127.0.0.1:8083

# Production defaults: DEBUG must stay disabled (assert_debug_disabled enforces on install/update)
APP_DEBUG=false
FLASK_DEBUG=0

# Phase 1 — Security hardening keys (2026-07-28)
PLUGIN_LICENSE_SECRET=${PLUGIN_LICENSE_SECRET}
CAPTCHA_SECRET_KEY=${CAPTCHA_SECRET_KEY}
DEV_ACCOUNTS_ENCRYPTION_KEY=${DEV_ACCOUNTS_ENCRYPTION_KEY}
LICENSE_SERVER_SECRET=${LICENSE_SERVER_SECRET}

# Deployments do not auto-install/enable plugins by default (install manually in admin)
PLUGIN_AUTO_INSTALL=0

# VeroGuard — daemon encrypted communication key (official side and client must match)
PROBE_SECRET=${PROBE_SECRET}

# Health check internal secret — 审计 D5：randomly generated, no longer using the publicly hardcoded default from the source
HEALTH_SECRET=${HEALTH_SECRET}

# 审计 M21：VeroGuard report-back address and deployment code (configure by region; CN deployments: fill in your regional API address,
# leave empty to use the global default https://api.verorun.com)
GUARDIAN_REMOTE_URL=
DEPLOYMENT_CODE=

# API Keys (intentionally empty — set real values before enabling AI features)
DASHSCOPE_TEXT_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=

# Automatic knowledge extraction gate (BUG-7). Off by default (P1-F07 security design);
# set to 1 when using cogevolution / evolution features.
AUTO_KNOWLEDGE_EXTRACT=0

# Structured JSON logs (X-Request-Id correlation) — shared/logging.py mounts the JSON file
# handler only when LOG_FILE is set (C-4 联调验证依赖此配置；目录不存在时应用自动创建)
LOG_FILE=${LOG_DIR}/verorun-app.jsonl

# Region routing (VeroRun 0.43.0+)
APP_REGION=${REGION}
ENVEOF

    # Education edition: mark edition + persist the deployment code for VeroGuard / auth plugins to read (written only for the edu type)
    if [ "${DEPLOY_TYPE}" = "edu" ]; then
        echo "VR_EDITION=edu" >> "${env_file}"
        echo "EDU_CODE=${EDU_CODE:-}" >> "${env_file}"
    fi

    # 桌面版分版（科研桌面版 research-desktop / 金融桌面版 finance-desktop / 官方版 official 等）：
    # 产物根目录的 .verorun-edition 标识驱动 VR_EDITION，供 agent_matrix._current_edition() 消费，
    # 驱动角色集与插件 overlay。标识存在时覆盖上面的 edu 旧分支（双保险）。
    if [ -f "${APP_HOME}/.verorun-edition" ]; then
        local _edition_marker
        _edition_marker=$(tr -d '[:space:]' < "${APP_HOME}/.verorun-edition")
        if [ -n "${_edition_marker}" ]; then
            if grep -q "^VR_EDITION=" "${env_file}"; then
                sed -i "s|^VR_EDITION=.*|VR_EDITION=${_edition_marker}|" "${env_file}"
            else
                echo "VR_EDITION=${_edition_marker}" >> "${env_file}"
            fi
        fi
    fi

    chown "${APP_USER}:${APP_USER}" "${env_file}"
    chmod 600 "${env_file}"
}

# ── Nginx: DOMAIN non-empty → multi-server for the domain; empty → single LAN server ──
write_nginx_config() {
    local nginx_conf="/etc/nginx/sites-available/verorun.conf"
    local nginx_enabled="/etc/nginx/sites-enabled/verorun.conf"

    # 审计 M7：Basic Auth for the admin panel at the nginx layer (defense in depth, disabled by default).
    # To enable: set ADMIN_BASIC_AUTH=1 + ADMIN_BASIC_AUTH_USER in .env, and generate htpasswd:
    #   sudo htpasswd -c /etc/nginx/.verorun-admin <ADMIN_BASIC_AUTH_USER>
    local _auth_basic_on=""
    if [ -f "${APP_HOME}/.env" ]; then
        local _admin_basic_auth
        local _admin_basic_user
        _admin_basic_auth=$(grep "^ADMIN_BASIC_AUTH=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
        _admin_basic_user=$(grep "^ADMIN_BASIC_AUTH_USER=" "${APP_HOME}/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
        if [ "${_admin_basic_auth}" = "1" ] && [ -n "${_admin_basic_user}" ] && [ -f "/etc/nginx/.verorun-admin" ]; then
            _auth_basic_on="    auth_basic \"VeroRun Admin\";
    auth_basic_user_file /etc/nginx/.verorun-admin;
"
        fi
    fi

    if [ -n "${DOMAIN:-}" ]; then
        # ── Domain mode: three servers for main / platform / agent ──
        # 审计 D3：built-in 443 + 80→443 redirect when a cert exists (TLS is not lost after update/configure-domain rewrites)
        local _cert_dir="/etc/letsencrypt/live/${DOMAIN}"
        local _ssl_listen=""
        local _ssl_cert=""
        local _http_redirect=""
        if [ -f "${_cert_dir}/fullchain.pem" ] && [ -f "${_cert_dir}/privkey.pem" ]; then
            _ssl_listen="    listen 443 ssl http2;"
            _ssl_cert="    ssl_certificate     ${_cert_dir}/fullchain.pem;
    ssl_certificate_key ${_cert_dir}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;"
            # 审计 D3+：redirect restricted to HTTP only (if in the same block as listen 443, https requests get 301-redirected to themselves → infinite loop)
            _http_redirect="    if (\$scheme != \"https\") { return 301 https://\$host\$request_uri; }"
        fi

        # F-08 修复：统一安全头组（nginx 单一层级，供 3 个 server 复用）。
        # HSTS 固定输出（不再依赖证书安装时序，解决部署后 Certbot 改写导致 HSTS 丢失）；
        # CSP 合并后端 CDN 白名单并移除 unsafe-eval；新增 Permissions-Policy。
        # 零预置地址：connect-src 已含 https: 通配，已移除硬编码业务子域 http://agent.verorun.com。
        #   业务地址一律来自部署时用户输入（DOMAIN 等变量），模板中不得预置任何域名或 IP。
        _sec_headers="    add_header X-Frame-Options \"SAMEORIGIN\" always;
    add_header Content-Security-Policy \"default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://static.cloudflareinsights.com; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: blob: https:; font-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self' ws: wss: https: https://api.github.com https://cdn.jsdelivr.net; frame-ancestors 'self'\" always;
    add_header X-Content-Type-Options \"nosniff\" always;
    add_header Referrer-Policy \"strict-origin-when-cross-origin\" always;
    add_header Permissions-Policy \"camera=(), microphone=(), geolocation=()\" always;
    add_header Strict-Transport-Security \"max-age=31536000; includeSubDomains\" always;"

        # 审计 P1-2：unknown Hosts rejected — default_server catch-all (prevents Host-header injection / cache poisoning).
        # 80 always; 443 only when a certificate exists (otherwise `listen 443 ssl` without a cert fails nginx -t).
        local _default_ssl_server=""
        if [ -f "${_cert_dir}/fullchain.pem" ] && [ -f "${_cert_dir}/privkey.pem" ]; then
            _default_ssl_server="
# 审计 P1-2：HTTPS catch-all — unknown Hosts on 443 also rejected
server {
    listen 443 ssl http2 default_server;
    server_name _;
    ssl_certificate     ${_cert_dir}/fullchain.pem;
    ssl_certificate_key ${_cert_dir}/privkey.pem;
    access_log off;
    return 444;
}"
        fi
        cat > "${nginx_conf}" << NGXEOF
# VeroRun Nginx — auto-generated by ${INSTALL_SCRIPT}

# 审计 M5：rate limiting for the auth/admin surfaces at the nginx layer (http-level zone, referenced by locations inside servers)
limit_req_zone \$binary_remote_addr zone=verorun_auth:10m rate=10r/s;
limit_req_status 429;   # D-14：超限返回 429 而非默认 503（避免客户端误判服务宕机）

# 审计 M9：access_log redaction — use \$uri (without query) instead of \$request,
# preventing URL query-string tokens such as JWT/sso_token from landing in logs (log leakage equals session hijacking)
log_format verorun_redact '\$remote_addr - \$remote_user [\$time_local] "\$request_method \$uri \$server_protocol" \$status \$body_bytes_sent "\$http_referer"';

# 审计 P1-2：unknown Hosts rejected — default_server catch-all (prevents Host-header injection / cache poisoning)
server {
    listen 80 default_server;
    server_name _;
    access_log off;
    return 444;
}
${_default_ssl_server}
# ── Main domain ────────────────────────────────
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};
    server_tokens off;
    access_log /var/log/nginx/verorun-access.log verorun_redact;
${_http_redirect}
${_ssl_listen}
${_ssl_cert}
${_sec_headers}

    # ── Admin ─────────────────────────────────
    location /admin/ {
        client_max_body_size 100M;
        limit_req zone=verorun_auth burst=20 nodelay;
${_auth_basic_on}
        proxy_pass http://127.0.0.1:8084;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }

    # 审计 D1：debug-jwt removed; nginx-layer defense in depth returns 404 directly
    location = /admin/debug-jwt {
        return 404;
    }

    # 审计 M17：HTML/SVG in upload directories forced to download, preventing stored XSS (regex has the highest priority)
    location ~* ^/admin/static/uploads/.*\.(html?|svg)$ {
        add_header Content-Disposition "attachment; filename=download" always;
        proxy_pass http://127.0.0.1:8084;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location ~* ^/static/uploads/.*\.(html?|svg)$ {
        add_header Content-Disposition "attachment; filename=download" always;
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location ~* ^/auth/static/uploads/.*\.(html?|svg)$ {
        add_header Content-Disposition "attachment; filename=download" always;
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # 审计：OAuth 第三方登录路由（/auth/oauth/*）须落到 8081 —— oauth_bp 仅注册于 auth_server
    location /auth/oauth/ {
        limit_req zone=verorun_auth burst=20 nodelay;
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # ── Auth / subscribe ─────────────────────
    location /auth/ {
        limit_req zone=verorun_auth burst=20 nodelay;
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /subscribe {
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # ── Main site ───────────────────────────────
    location / {
        # 审计 M4：main-site uploads (avatars up to 2MB etc.) need > nginx's default 1M, consistent with the backend limit
        client_max_body_size 100M;
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }

    # 审计 M8：WebSocket support (mini-program wss handshake requires HTTP/1.1 + Upgrade headers)
    location ~ ^/(ws|socket\.io)(/|$) {
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}

# ── Platform subdomain ─────────────────────────
server {
    listen 80;
    server_name platform.${DOMAIN};
    server_tokens off;
    access_log /var/log/nginx/verorun-access.log verorun_redact;
${_http_redirect}
${_ssl_listen}
${_ssl_cert}
${_sec_headers}

    location / {
        # 审计 M4：platform uploads consistent with the main site, body limit 100M
        client_max_body_size 100M;
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# ── Agent subdomain ────────────────────────────
server {
    listen 80;
    server_name agent.${DOMAIN};
    server_tokens off;
    access_log /var/log/nginx/verorun-access.log verorun_redact;
${_http_redirect}
${_ssl_listen}
${_ssl_cert}
    client_max_body_size 100M;
${_sec_headers}

    # 审计 P1-2/P2-4：debug-jwt removed; nginx-layer defense in depth returns 404 directly (agent subdomain too)
    location = /admin/debug-jwt {
        return 404;
    }

    location / {
        proxy_pass http://127.0.0.1:8084;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGXEOF
    else
        # ── No-domain mode: single default_server server (/admin/ uniformly includes client_max_body_size 100M, 审计 R4 L-B) ──
        # 审计 M11：detect local LAN IPs for the server_name whitelist (unknown Hosts hit a 444 rejection)
        local _lan_ips=""
        _lan_ips=$(hostname -I 2>/dev/null | tr -s ' ' | sed 's/^ //;s/ $//')
        # STD-5 修复（2026-08-29 标准版部署测试）：回环地址加入白名单——
        # 之前本机 curl http://127.0.0.1/ 落入 default_server 444（空回复），
        # 服务器本地健康验证与脚本探测全部拿到 000。
        local _lan_server_name="localhost 127.0.0.1"
        [ -n "${_lan_ips}" ] && _lan_server_name="${_lan_server_name} ${_lan_ips}"
        # STD-8 修复（2026-08-29 自签 HTTPS 产品化）：no-domain 模式感知自签证书——
        # 证书由 deploy/intranet/setup_lan_selfsigned.sh 生成；证书存在时主 server 追加 443，
        # update/configure 重写后 HTTPS 不丢失（与 domain mode D3 同理）。
        # 443 不设 default_server 444：IP 直连 TLS Client Hello 无 SNI，若 443 default_server 为 444
        # 兜底块会破坏 https://<IP> 访问（复检报告取舍，未知 Host 在 443 落入主块为残余硬化缺口）。
        local _lan_ssl_listen=""
        local _lan_ssl_cert=""
        if [ -f "/etc/verorun/certs/lanip/fullchain.pem" ] && [ -f "/etc/verorun/certs/lanip/privkey.pem" ]; then
            _lan_ssl_listen="    listen 443 ssl http2;"
            _lan_ssl_cert="    ssl_certificate     /etc/verorun/certs/lanip/fullchain.pem;
    ssl_certificate_key /etc/verorun/certs/lanip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;"
        fi
        cat > "${nginx_conf}" << NGXEOF
# VeroRun Nginx — no-domain mode (auto-generated by ${INSTALL_SCRIPT})

# 审计 M5：rate limiting for the auth/admin surfaces at the nginx layer (http-level zone, referenced by locations inside servers)
limit_req_zone \$binary_remote_addr zone=verorun_auth:10m rate=10r/s;
limit_req_status 429;   # D-14：超限返回 429 而非默认 503（避免客户端误判服务宕机）

# 审计 M9：access_log redaction — use \$uri (without query) instead of \$request, preventing token leakage into logs
log_format verorun_redact '\$remote_addr - \$remote_user [\$time_local] "\$request_method \$uri \$server_protocol" \$status \$body_bytes_sent "\$http_referer"';

server {
    listen 80;
${_lan_ssl_listen}
${_lan_ssl_cert}
    # 审计 M11：restrict server_name to the local localhost/LAN IPs; unknown Hosts no longer hit this block
    server_name ${_lan_server_name};
    server_tokens off;
    access_log /var/log/nginx/verorun-access.log verorun_redact;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self' ws: wss: https:; frame-ancestors 'self'" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # ── Admin panel ─────────────────────────
    location /admin/ {
        client_max_body_size 100M;
        limit_req zone=verorun_auth burst=20 nodelay;
${_auth_basic_on}
        proxy_pass http://127.0.0.1:8084;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }

    # 审计 D1：debug-jwt removed; nginx-layer defense in depth returns 404 directly
    location = /admin/debug-jwt {
        return 404;
    }

    # 审计 M17：HTML/SVG in upload directories forced to download, preventing stored XSS (regex has the highest priority)
    location ~* ^/admin/static/uploads/.*\.(html?|svg)$ {
        add_header Content-Disposition "attachment; filename=download" always;
        proxy_pass http://127.0.0.1:8084;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location ~* ^/static/uploads/.*\.(html?|svg)$ {
        add_header Content-Disposition "attachment; filename=download" always;
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location ~* ^/auth/static/uploads/.*\.(html?|svg)$ {
        add_header Content-Disposition "attachment; filename=download" always;
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # ── User console / auth ─────────────────
    location /auth/ {
        limit_req zone=verorun_auth burst=20 nodelay;
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /subscribe {
        proxy_pass http://127.0.0.1:8083;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # ── Main site (default route) ───────────
    location / {
        # 审计 M4：LAN main-site uploads consistent with the backend limit
        client_max_body_size 100M;
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        # 审计 M1：XFF overwritten with the direct IP (\$remote_addr), not appending client-forged values
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }

    # 审计 M8：WebSocket support (mini-program wss handshake requires HTTP/1.1 + Upgrade headers)
    location ~ ^/(ws|socket\.io)(/|$) {
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}

# 审计 M11：unknown Hosts rejected (prevents Host-header injection / cache poisoning) — default_server as the catch-all
server {
    listen 80 default_server;
    server_name _;
    access_log off;
    return 444;
}
NGXEOF
    fi

    # 审计 M-2 fix：back up the default site before removing it (idempotent — an existing backup is not overwritten),
    # avoiding collateral damage to other web services on the same host (phpMyAdmin/Grafana etc.) that depend on the default config.
    if [ -f /etc/nginx/sites-available/default ] && [ ! -f /etc/nginx/sites-available/default.bak.verorun ]; then
        cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.bak.verorun
    fi
    rm -f /etc/nginx/sites-enabled/default
    ln -sf "${nginx_conf}" "${nginx_enabled}"
}

# ── Fresh install: DEPLOY_TYPE drives domain prompt / pull messaging / cleanup / service startup ──
do_install() {
    # 审计 DIR-1 fix：resolve_directory_conflict 必须最先执行，早于任何可能隐式创建
    # APP_HOME 的步骤。此前置于 "Create directories"（旧 WEB-2）仍不够早——
    # "Node.js/miniprogram-ci"（npm install --prefix ${APP_HOME}/plugins/... 会自动
    # mkdir -p 整条路径链，含 APP_HOME 根）先于它运行，导致全新安装时 APP_HOME 被
    # 提前创建，冲突检测误判 "exists but not git repo" 而中止（.104 复现）。
    resolve_directory_conflict "${APP_HOME}"

    step "Dependency check"
    if [ "${SKIP_DEPS:-0}" = "1" ]; then
        echo -e "${WARN} --skip-deps: skipping dependency installation"
    elif check_system_deps && check_python_deps; then
        echo -e "${OK} All dependencies already installed — skipping"
        SKIP_DEPS=1
    else
        echo -e "${WARN} Some dependencies are missing (system or Python packages)"
        # 审计 H-2 fix：when run via a curl | sudo bash pipe, stdin is already consumed by the script content,
        # so reads must use < /dev/tty or read will swallow the rest of the script.
        # The prompt uses echo -n > /dev/tty instead — read -p prompts go to stderr,
        # and under a 2>&1 | tail pipe they get swallowed by buffering, appearing to hang.
        # 审计 F-4（2026-08-30 复测）：非交互/CI 下 /dev/tty 不存在，重定向打开失败会先向 stderr
        # 报 "没有那个设备或地址"（bash 从左到右处理重定向，2>/dev/null 来不及吞掉打开失败），
        # 产生 2 条观感像出错的噪音。先探测 tty：无 tty 直接默认继续安装，不写 /dev/tty。
        _has_tty=0
        if { exec 3<>/dev/tty; } 2>/dev/null; then
            exec 3>&-
            _has_tty=1
        fi
        if [ "${_has_tty}" = "1" ]; then
            echo -n "Install dependencies now? [Y/n] " > /dev/tty
            read -r _ans < /dev/tty || _ans=""
        else
            echo -e "${INFO} Non-interactive mode — auto-continuing with dependency installation"
            _ans=""
        fi
        case "${_ans}" in
            n|N) echo -e "${WARN} Skipping dependency installation"; SKIP_DEPS=1 ;;
            *)   echo -e "${OK} Will install missing dependencies" ;;
        esac
    fi
    done_step "Dependency check complete"

    step "System dependencies"
    export DEBIAN_FRONTEND=noninteractive
    if [ "${SKIP_DEPS:-0}" != "1" ]; then
        _ensure_apt_mirror
        apt-get update
        apt-get install -y python3 python3-venv python3-pip python3-dev \
            nginx git curl wget build-essential libpq-dev libssl-dev
        # 审计 P-1：requirements.lock needs Python >= 3.12 (numpy 2.4.x); the version is guaranteed automatically
        _ensure_python312 || {
            echo -e "${FAIL} Python 3.12 setup failed — fix the error above and re-run."
            exit 1
        }
    else
        if [ "${DEPLOY_TYPE}" = "production" ]; then
            echo -e "${WARN} --skip-deps: skipping system dependency installation"
        else
            echo -e "${WARN} Skipped (deps already present or --skip-deps)"
        fi
    fi
    done_step "System dependencies installed"

    step "Node.js runtime + miniprogram-ci (WeChat auto-upload)"
    # miniprogram-ci 需 Node >= 16；缺失或安装失败时微信上传自动降级为手动指引，
    # 不阻断主安装。幂等：node/npm 与 node_modules 已存在时直接跳过。
    local _node_ok=0 _node_major=""
    if command -v node >/dev/null 2>&1; then
        _node_major="$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')"
        [ -n "${_node_major}" ] && [ "${_node_major}" -ge 16 ] 2>/dev/null && _node_ok=1
    fi
    if [ "${_node_ok}" != "1" ] && [ "${SKIP_DEPS:-0}" = "1" ]; then
        echo -e "${WARN} --skip-deps: Node.js >= 16 missing — WeChat auto-upload will degrade to manual upload (run: sudo apt-get install -y nodejs npm)"
    elif [ "${_node_ok}" != "1" ]; then
        if ! timeout 300 apt-get install -y nodejs npm 2>&1; then
            echo -e "${WARN} nodejs install failed — WeChat auto-upload will degrade to manual upload"
        elif command -v node >/dev/null 2>&1 && [ "$(node --version | sed 's/^v//; s/\..*//')" -ge 16 ]; then
            _node_ok=1
        fi
    fi
    if [ "${_node_ok}" = "1" ]; then
        local _mp_ci_dir="${APP_HOME}/plugins/mini_app_builder/submodules/publish/services"
        if [ -d "${_mp_ci_dir}/node_modules/miniprogram-ci" ]; then
            echo -e "${OK} miniprogram-ci already installed"
        elif timeout 600 npm install --prefix "${_mp_ci_dir}" miniprogram-ci 2>&1; then
            echo -e "${OK} miniprogram-ci installed for WeChat auto-upload"
        else
            echo -e "${WARN} miniprogram-ci install failed — WeChat auto-upload will degrade to manual upload"
        fi
    fi
    done_step "Node.js/miniprogram-ci ready"

    : "${PG_PASSWORD:=$(python3 -c "import secrets; print(secrets.token_hex(16))")}"

    step "PostgreSQL"
    if ! systemctl is-active --quiet postgresql 2>/dev/null; then
        if [ "${SKIP_DEPS:-0}" = "1" ]; then
            echo -e "${FAIL} postgresql not running, but dependency installation was skipped"
            exit 1
        fi
        apt-get install -y postgresql postgresql-client
        systemctl enable --now postgresql
    fi
    # 审计 C1 satisfied：password passed to psql via pipe (stdin), never appearing in the process command line (psql argv is only "-q")
    # 审计 Y-4 fix：the server's fs.protected_regular=2 (writing other users' files inside sticky world-writable
    # directories is forbidden, even for root) made mktemp→chown postgres→printf file writes fail with kernel EACCES.
    # Switch to a stdin pipe instead — no file, no owner, no /tmp, fully decoupled from the kernel protection.
    local _pwd="${PG_PASSWORD//\'/\'\'}"
    local _db="appdb" _role="app" _tries=0
    # 审计 M8 fix：creating the role / changing the password / creating the database no longer swallow errors silently — each step
    # checks its output explicitly, and on success a real TCP connection must be tested with the .env PG_PASSWORD (pg_hba enforces
    # password auth for host rules, same as the app runs); on failure, disconnect→DROP→recreate and retry, permanently fixing "password authentication failed".
    while [ "${_tries}" -lt 2 ]; do
        if sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${_role}'" 2>/dev/null | grep -qE '^\s*1\s*$'; then
            printf "ALTER ROLE %s WITH LOGIN PASSWORD '%s';\n" "${_role}" "${_pwd}" | sudo -u postgres psql -q 2>&1 \
                || echo -e "${WARN} ALTER ROLE failed (attempt $((_tries + 1))/2)"
        else
            printf "CREATE ROLE %s WITH LOGIN PASSWORD '%s';\n" "${_role}" "${_pwd}" | sudo -u postgres psql -q 2>&1 \
                || echo -e "${WARN} CREATE ROLE failed (attempt $((_tries + 1))/2)"
        fi
        if ! sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${_db}'" 2>/dev/null | grep -qE '^\s*1\s*$'; then
            sudo -u postgres createdb -O "${_role}" "${_db}" 2>&1 \
                || echo -e "${WARN} createdb failed (attempt $((_tries + 1))/2)"
        fi
        # Real password test: connect via the same TCP auth path as the application to verify the .env password matches the actual DB password
        if PGPASSWORD="${PG_PASSWORD}" psql -h localhost -p "${PG_PORT:-5432}" -U "${_role}" -d "${_db}" -tAc "SELECT 1" >/dev/null 2>&1; then
            break
        fi
        _tries=$((_tries + 1))
        if [ "${_tries}" -lt 2 ]; then
            echo -e "${WARN} Password check failed (attempt ${_tries}/2) — dropping ${_db} & ${_role} and recreating"
            sudo -u postgres psql -q -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${_db}' AND pid <> pg_backend_pid()" >/dev/null 2>&1 || true
            sudo -u postgres psql -c "DROP DATABASE IF EXISTS ${_db}" >/dev/null 2>&1 || true
            sudo -u postgres psql -c "DROP ROLE IF EXISTS ${_role}" >/dev/null 2>&1 || true
        fi
    done
    if [ "${_tries}" -ge 2 ]; then
        echo -e "${FAIL} FATAL: role/database password verification failed after 2 attempts."
        echo -e "${INFO} Manual recovery:"
        echo -e "${INFO}   sudo -u postgres psql"
        echo -e "${INFO}     DROP DATABASE IF EXISTS appdb;"
        echo -e "${INFO}     DROP ROLE IF EXISTS app;"
        echo -e "${INFO}     CREATE ROLE app WITH LOGIN PASSWORD '${PG_PASSWORD}';"
        echo -e "${INFO}     CREATE DATABASE appdb OWNER app;"
        exit 1
    fi
    # Iron rule: the install script only creates the system database; plugin databases are never created.
    # (2026-08-21) site_builder / mini_app_builder 独立数据库豁免已取消，回归独立 schema，不再创建任何插件库。
    # pgvector 平台能力（2026-08-21 重设计）：扩展创建已下沉到插件迁移 SQL
    # （CREATE EXTENSION IF NOT EXISTS vector SCHEMA public，幂等），本脚本不再以超级用户
    # 无条件预建扩展——避免为未订阅插件的用户强制写入 vector 扩展（部署物垃圾）。
    # 本脚本仅保证平台能力存在（对插件透明，第三方插件同样受益）：
    #   (1) pgvector 二进制已安装（缺 vector.control 时给出安装指引，不中断安装）
    #   (2) 控制文件直接新增 trusted = true（幂等，追加前先检查），
    #       使 DB owner（app）无需 superuser 即可在插件激活时按需自建扩展
    _vector_ctl="$(ls /usr/share/postgresql/*/extension/vector.control 2>/dev/null | head -1 || true)"
    if [ -z "${_vector_ctl}" ]; then
        echo -e "${WARN} pgvector 二进制未安装（未找到 vector.control）— 依赖插件将在激活时缺少扩展（请安装 PGDG 的 postgresql-XX-pgvector）"
    elif ! grep -q '^trusted' "${_vector_ctl}" 2>/dev/null; then
        printf '\ntrusted = true\n' >> "${_vector_ctl}"
        echo -e "${OK} pgvector platform capability: trusted = true added (${_vector_ctl})"
    else
        echo -e "${INFO} pgvector already trusted"
    fi
    done_step "PostgreSQL is running"

    step "Create directories"
    # 审计 DIR-1 fix：resolve_directory_conflict 已提升至 do_install() 最开头执行，
    # 早于 npm 隐式创建 APP_HOME。此处不再重复调用，直接创建目录结构。
    mkdir -p "${APP_HOME}" "${APP_HOME}/data" "${LOG_DIR}"
    mkdir -p "${APP_HOME}/.cache/llm" \
             "${APP_HOME}/.cache/sessions" \
             "${APP_HOME}/.cache/agents"
    chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}" 2>/dev/null || true
    chown -R "${APP_USER}:${APP_USER}" "${LOG_DIR}" 2>/dev/null || true
    done_step "Directories ready"

    # Git repo auto-resolution (审计 Y-1)：automatically switch to a mirror when HTTPS direct is unreachable; SSH uses 443
    _resolve_git_repo

    ensure_git_auth

    # ── DEPLOY_TYPE driven: Pull code step label (commit hash evaluated dynamically after the pull completes) ──
    local _pull_step="Pull code"
    local _pull_suffix=""           # 审计 R5 BUG-1：stores only the format suffix; no command substitution here
    case "${DEPLOY_TYPE}" in
        code)
            _pull_step="Pull code (full — includes all plugins)"
            _pull_suffix=" (full, all plugins)"
            ;;
        dev)
            _pull_step="Pull code (plugins excluded — clone ~50% smaller)"
            _pull_suffix=" (plugins excluded)"
            ;;
    esac
    step "${_pull_step}"
    # 审计 H3 fix：interactive three-way choice on directory conflict (backup/delete/abort), no longer a direct rm -rf
    # 目录冲突检测已提前到 "Create directories" 步骤（WEB-2 fix），此处不再重复调用。
    if [ -d "${APP_HOME}/.git" ]; then
        git config --global --add safe.directory "${APP_HOME}" 2>/dev/null || true
        cd "${APP_HOME}"
        # 审计 F-2：suppress git interactive credential prompts + timeout protection, avoiding infinite stalls when origin points to a mirror
        git remote set-url origin "${GIT_REPO}"
        export GIT_TERMINAL_PROMPT=0
        # 无人值守加固 ①：pre-warm known_hosts so git-over-SSH never stalls on host-key verification under `sudo`.
        # Without this, a fresh root environment with an empty /root/.ssh/known_hosts blocks git fetch until the timeout.
        ensure_git_auth
        _fetch_ok=0
        for _i in 1 2 3; do
            if timeout "${GIT_TIMEOUT}" git fetch origin "${GIT_BRANCH}" 2>&1; then
                _fetch_ok=1
                break
            fi
            echo -e "${WARN} Git fetch failed or timed out (attempt ${_i}/3) — retrying"
            sleep $((5 * _i))
        done
        if [ "${_fetch_ok}" != "1" ]; then
            echo -e "${FAIL} Git fetch failed after 3 attempts (${GIT_TIMEOUT}s timeout each) — aborting"
            echo -e "${INFO} Check origin remote: git -C ${APP_HOME} remote -v"
            echo -e "${INFO} If it points to a mirror (ghfast.top/ghproxy), reset it:"
            echo -e "${INFO}   git -C ${APP_HOME} remote set-url origin ${GIT_REPO}"
            exit 1
        fi
        git reset --hard "origin/${GIT_BRANCH}"
    else
        # 审计 F-3（2026-08-30 标准版复测）：全新安装时 "Create directories" 已 mkdir 出
        # APP_HOME/data 与 .cache 等框架目录，git clone 目标非空导致首试必败、靠重试自愈
        # （多耗约 15s 并输出误导性 WARN）。此分支的 APP_HOME 必为本安装创建
        # （resolve_directory_conflict 已前置处理历史冲突），先清空再 clone，一次成功。
        if [ -d "${APP_HOME}" ]; then
            find "${APP_HOME}" -mindepth 1 -delete
        fi
        _clone_with_timeout "${GIT_REPO}" "${APP_HOME}" "${GIT_BRANCH}"
    fi
    # Apply the sparse-checkout whitelist ONLY when SPARSE_DIRS is non-empty.
    # 根治 2026-08-20：SPARSE_DIRS 为空 = 全量检出（官方版/源码版），只执行 disable，
    # 绝不 init --cone + set —— 否则旧白名单会在 git pull 时删除 cone 外的被跟踪文件
    # （曾导致官方版 26 个插件被清空、服务加载失败）。
    if [ -n "${SPARSE_DIRS:-}" ]; then
        # 审计 H6：leftovers from old repos/manual mode (core.sparseCheckoutCone not set) make set follow manual mode,
        # where patterns contain only directories with no "/*" root-file keep rule → root files such as
        # requirements.txt/VERSION/README get deleted from the working tree. First disable to clear leftovers, then init --cone + set;
        # on failure fall back to a full checkout (no files deleted) and continue installation.
        git -C "${APP_HOME}" sparse-checkout disable 2>/dev/null || true
        if git -C "${APP_HOME}" sparse-checkout init --cone 2>/dev/null \
            && git -C "${APP_HOME}" sparse-checkout set ${SPARSE_DIRS} 2>/dev/null; then
            :
        else
            git -C "${APP_HOME}" sparse-checkout disable 2>/dev/null || true
            echo -e "${WARN} sparse-checkout failed — keeping full working tree"
        fi
    else
        git -C "${APP_HOME}" sparse-checkout disable 2>/dev/null || true
        echo -e "${INFO} SPARSE_DIRS empty — full checkout, sparse-checkout disabled"
    fi
    # No-domain scripts (install-local/code/dev) are kept on disk: deleting
    # git-tracked files leaves unstaged changes that break `git pull` / update.
    # Clean stale __pycache__ before chown (avoids race-condition failures)
    find "${APP_HOME}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}" 2>/dev/null || true
    done_step "Code pulled${_pull_suffix}: $(git -C "${APP_HOME}" log --oneline -1)"

    # STD-3 前置预检（2026-08-29 标准版部署测试）：发版清单与 VERSION 的 semver 一致性必须在
    # 重量级步骤（venv + pip 全量依赖，实测 5-10 分钟）之前校验——实测 0.60.0 VERSION 搭配
    # 0.59.9 已签名清单时，安装在 pip 全部完成后才被 verify_release_manifest fail-closed 拒绝，
    # 白白浪费整段下载安装时间。此处仅用系统 python3 做轻量 json 读取；深度验签仍由下方
    # verify_release_manifest / build_veroguard_manifest 执行。
    if [ -f "${APP_HOME}/release_manifest.json" ] && [ -f "${APP_HOME}/VERSION" ]; then
        local _pre_mf_semver="" _pre_ver=""
        _pre_mf_semver=$(python3 -c "import json;print(json.load(open('${APP_HOME}/release_manifest.json',encoding='utf-8')).get('semver',''))" 2>/dev/null || true)
        _pre_ver=$(tr -d '[:space:]' < "${APP_HOME}/VERSION")
        if [ -n "${_pre_mf_semver}" ] && [ -n "${_pre_ver}" ] && [ "${_pre_mf_semver}" != "${_pre_ver}" ]; then
            echo -e "${FAIL} release_manifest semver ${_pre_mf_semver} != VERSION ${_pre_ver}（发版清单与代码版本错位）"
            echo -e "${INFO} 维护者修复：用 RELEASE_SIGN_KEY 运行 python3 deploy/scripts/sign_release.py 重签并推送；"
            echo -e "${INFO} 或改用与签名清单一致的版本分支/标签安装。已在依赖安装之前中止（fail-fast）。"
            exit 1
        fi
    fi

    step "Python virtual environment"
    if [ "${SKIP_DEPS:-0}" != "1" ]; then
        # 审计 P-1：automatically rebuilds when the venv is missing or its Python < 3.12 (venv holds no business data, safe)
        _ensure_venv || {
            echo -e "${FAIL} Python venv setup failed — fix the error above and re-run."
            exit 1
        }
        _pip_install --upgrade pip
        # 审计 M16：prefer the hash-locked requirements.lock (reproducible builds), fall back to requirements.txt when missing
        local _req_file="${APP_HOME}/requirements.txt"
        [ -f "${APP_HOME}/requirements.lock" ] && _req_file="${APP_HOME}/requirements.lock"
        _pip_install -r "${_req_file}"
    else
        echo -e "${WARN} Skipped (deps already present or --skip-deps)"
    fi
    done_step "Python dependencies installed"

    # Only production requires a domain
    if [ "${DEPLOY_TYPE}" = "production" ]; then
        prompt_domain
    fi

    step "Generate .env"
    generate_env force
    if [ "${DEPLOY_TYPE}" = "production" ]; then
        done_step ".env generated"
    else
        done_step ".env generated (DEPLOY_DOMAIN empty, DEPLOY_PROTOCOL=http)"
    fi

    # 审计 NEW-H1：VeroGuard integrity manifest build consistent with the four scripts
    verify_release_manifest
    build_veroguard_manifest

    # Production gate: refuse to continue if DEBUG got enabled in .env (dev type exempt — debug on by design)
    if [ "${DEPLOY_TYPE}" != "dev" ]; then
        assert_debug_disabled
    fi

    # Only production: do not start systemd / nginx when DOMAIN is not configured
    if [ "${DEPLOY_TYPE}" = "production" ] && [ -z "${DOMAIN}" ]; then
        echo -e "${WARN} Domain not configured. System and nginx not started."
        echo -e "${INFO} After install, run:"
        echo -e "${INFO}   sudo bash deploy/${INSTALL_SCRIPT} configure-domain <your-domain>"
    else
        step "systemd services"
        write_systemd_services
        done_step "systemd services configured"

        if [ "${DEPLOY_TYPE}" = "production" ]; then
            step "Nginx"
        else
            step "Nginx (path routing)"
        fi
        write_nginx_config
        nginx -t && systemctl restart nginx
        done_step "Nginx configured"

        # 审计 D-3 fix：无域名 + IP + CERT_SOURCE=private_ca 时自动运行内网自签脚本
        # （setup_private_ca_ip.sh），让 HTTPS 在服务启动前就绪，实现真·一键，免去
        # 「install → IP 证书脚本 → 重启」三步法。失败则降级为 http 并提示手动补跑。
        if [ "${CERT_SOURCE:-}" = "private_ca" ] \
           && echo "${DOMAIN:-}" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' \
           && [ ! -f "/etc/verorun/certs/lanip/fullchain.pem" ]; then
            echo -e "${INFO} IP domain + private_ca detected — auto-running intranet cert script (setup_private_ca_ip.sh)"
            if [ -f "${APP_HOME}/deploy/intranet/setup_private_ca_ip.sh" ]; then
                if APP_HOME="${APP_HOME}" bash "${APP_HOME}/deploy/intranet/setup_private_ca_ip.sh" --ip "${DOMAIN}"; then
                    echo -e "${OK} Intranet self-signed HTTPS certificate ready"
                else
                    echo -e "${WARN} setup_private_ca_ip.sh failed — HTTPS deferred, run it manually after install"
                fi
            else
                echo -e "${WARN} intranet/setup_private_ca_ip.sh not found — HTTPS deferred, run it manually after install"
            fi
        fi

        step "Start services"
        restart_services
        done_step "Services started"

        # Wait for backends to be ready before SSL cert (avoid 502 on HTTP challenge)
        _wait=0
        _max_wait=30
        while [ $_wait -lt $_max_wait ]; do
            if curl -s --max-time 2 http://127.0.0.1:8081/ > /dev/null 2>&1; then
                if _skip_user_console \
                   || curl -s --max-time 2 http://127.0.0.1:8083/ > /dev/null 2>&1; then
                    echo -e "${OK} Backend services ready"
                    break
                fi
            fi
            sleep 1
            _wait=$((_wait + 1))
        done
        if [ $_wait -ge $_max_wait ]; then
            echo -e "${WARN} Backends did not respond within ${_max_wait}s — SSL may fail"
        fi
    fi

    # Automatic HTTPS certificate issuance (审计 Y-2)：enabled only for production + when the domain is configured
    _setup_ssl_cert

    # 审计 P0-1：certificate issuance failed / absent → rewrite DEPLOY_PROTOCOL back to http.
    # generate_env writes https for production+DOMAIN, and _setup_ssl_cert only upgrades to https on success;
    # if the cert is missing (certbot failed / DNS not resolving / offline), keeping https makes the app emit
    # Secure SSO cookies that browsers drop over plain HTTP → login sessions die. Rewrite to http here.
    if [ "${DEPLOY_TYPE}" = "production" ] && [ -n "${DOMAIN:-}" ]; then
        # 审计 D-3 fix：IP 场景证书位于 /etc/verorun/certs/lanip/（setup_private_ca_ip.sh），
        # 不能仅因 /etc/letsencrypt/live/${DOMAIN} 无证书就把 https 回退为 http。
        local _tls_ok=0
        [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ] && _tls_ok=1
        if [ "${CERT_SOURCE:-}" = "private_ca" ] \
           && echo "${DOMAIN}" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' \
           && [ -f "/etc/verorun/certs/lanip/fullchain.pem" ]; then
            _tls_ok=1
        fi
        if [ "${_tls_ok}" != "1" ] \
            && grep -q "^DEPLOY_PROTOCOL=https" "${APP_HOME}/.env" 2>/dev/null; then
            sed -i "s/^DEPLOY_PROTOCOL=.*/DEPLOY_PROTOCOL=http/" "${APP_HOME}/.env"
            echo -e "${WARN} TLS certificate not found at /etc/letsencrypt/live/${DOMAIN} — DEPLOY_PROTOCOL set back to http."
            echo -e "${INFO} SSO cookie Secure flag disabled; run certbot manually to enable HTTPS."
        fi
    fi

    step "Configure sudoers (one-click update permissions)"
    write_sudoers
    done_step "Sudoers configured"

    step "Database migration"
    if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
        sudo -u "${APP_USER}" bash -c "set -a; source ${APP_HOME}/.env; cd ${APP_HOME} && PYTHONPATH=${APP_HOME}:${APP_HOME}/auth-center ${VENV_DIR}/bin/python -c 'from models.database import init_db; init_db()'"
        done_step "Database migrated"
    else
        echo -e "${WARN} Skipped database migration (pass --approve-migrate to apply schema changes)"
        echo -e "${INFO} Services may fail to start if code references columns not yet in the DB"
    fi

    # 标准版内置插件建表（2026-08-29）：email/sms/im_gateway 为运行时幂等建表，
    # 此前仅挂在插件 enable 生命周期钩子上，而部署默认不自动启用插件（PLUGIN_AUTO_INSTALL=0）
    # → 插件表不创建，管理员后手启用插件时相关功能直接 500。
    # 此处随主库迁移一并提前建表（幂等、无副作用）；captcha_embedded 无表（Redis/内存存储）。
    # 逐插件 try/except：某插件缺失或连接失败不阻断整体安装（有界原则，禁止 || true 掩盖后单独跳过）。
    step "Bundled plugin tables"
    if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
        sudo -u "${APP_USER}" bash -c "set -a; source ${APP_HOME}/.env; cd ${APP_HOME} && PYTHONPATH=${APP_HOME}:${APP_HOME}/auth-center ${VENV_DIR}/bin/python - <<'PY'
def _init(name, module, func):
    try:
        mod = __import__(module, fromlist=[func])
        getattr(mod, func)()
        print('[BUNDLED] %s tables ready' % name)
    except Exception as e:
        print('[BUNDLED] %s tables: skip (%s)' % (name, str(e)[:120]))
_init('email', 'plugins.email.models', 'init_email_db')
_init('sms', 'plugins.sms.models', 'init_sms_db')
_init('im_gateway', 'plugins.im_gateway.models', 'init_im_db')
PY"
        done_step "Bundled plugin tables ready"
    else
        echo -e "${WARN} Skipped bundled plugin tables (pass --approve-migrate to apply schema changes)"
    fi

    step "Seed data"
    # 审计 NEW-M1：credentials are uniformly generated and echoed by common.sh do_seed (no longer passed via global variables here)
    do_seed
    done_step "Seed data injected"

    print_summary
}

# ── Incremental update: production reads the domain from .env; missing .git handled per mode; self-update uses ${INSTALL_SCRIPT} ──
do_update() {
    # ── Trap: write failure status on any early exit ──
    # /run/verorun/ is tmpfs managed by systemd RuntimeDirectory (verorun-admin.service).
    # Owned by APP_USER, no root-permission conflicts. Cleared on reboot (intended).
    local _status_file="/run/verorun/update_status.json"
    mkdir -p /run/verorun 2>/dev/null || true
    chown "${APP_USER}:${APP_USER}" /run/verorun 2>/dev/null || true
    trap 'echo "{\"status\":\"failed\",\"progress\":100,\"message\":\"Update failed\",\"error\":\"Script exited unexpectedly\"}" > '"${_status_file}" EXIT

    # Self-update tracking: md5 of currently-running ${INSTALL_SCRIPT}
    UPDATE_MD5=$(md5sum "${APP_HOME}/deploy/${INSTALL_SCRIPT}" 2>/dev/null | awk '{print $1}') || UPDATE_MD5=""

    # Only production reads the domain from .env; no-domain modes keep DOMAIN empty (write_nginx_config uses default_server)
    if [ "${DEPLOY_TYPE}" = "production" ]; then
        DOMAIN=$(grep "^DEPLOY_DOMAIN=" "${APP_HOME}/.env" 2>/dev/null | tail -1 | cut -d= -f2) || true
    fi

    local before_commit
    before_commit=$(git -C "${APP_HOME}" log --oneline -1 2>/dev/null || echo "unknown")

    step "Backup current version"
    mkdir -p "${APP_HOME}/.rollback"
    cp "${APP_HOME}/.env" "${APP_HOME}/.rollback/.env.bak" 2>/dev/null || true
    echo "${before_commit}" > "${APP_HOME}/.rollback/before_commit"
    done_step "Environment backed up"

    step "Restore locally modified files"
    # 审计 C-3：by default refuse to overwrite local modifications (prevents destroying user customizations/hot fixes); with --force, back up the diff first, then restore
    if [ -d "${APP_HOME}/.git" ]; then
        if ! git -C "${APP_HOME}" diff --quiet; then
            if [ "${FORCE_UPDATE:-0}" != "1" ]; then
                echo -e "${FAIL} Local modifications detected — refusing to overwrite them."
                echo -e "${INFO} To review:  git -C ${APP_HOME} diff"
                echo -e "${INFO} To backup:  git -C ${APP_HOME} diff > ${APP_HOME}/.rollback/local-patch-$(date +%s).diff"
                echo -e "${INFO} Re-run with '--force' to auto-restore (a backup diff is saved first)."
                exit 1
            fi
            mkdir -p "${APP_HOME}/.rollback"
            git -C "${APP_HOME}" diff > "${APP_HOME}/.rollback/local-patch-$(date +%s).diff"
            echo -e "${WARN} Local modifications detected (backed up to .rollback/local-patch-*.diff), restoring to git version..."
            git -C "${APP_HOME}" diff --name-only -z | xargs -0 git -C "${APP_HOME}" checkout --
            done_step "Locally modified files restored (diff saved)"
        else
            done_step "No local modifications"
        fi
    else
        done_step "Skipped (no .git directory)"
    fi

    # Git repo auto-resolution (审计 Y-1)：automatically switch to a mirror when HTTPS direct is unreachable; SSH uses 443
    _resolve_git_repo

    ensure_git_auth

    step "Pull latest code"
    if [ ! -d "${APP_HOME}/.git" ]; then
        if [ "${DEPLOY_TYPE}" = "production" ]; then
            echo -e "${WARN} .git missing — re-cloning repository"
            # 审计 H3 fix：reuse the interactive conflict handler; direct rm -rf is forbidden
            resolve_directory_conflict "${APP_HOME}"
            _clone_with_timeout "${GIT_REPO}" "${APP_HOME}" "${GIT_BRANCH}"
        else
            # 审计 R4 L-A：error messages use ${INSTALL_SCRIPT}, no longer hardcoding the script name
            echo -e "${FAIL} .git missing — cannot update. Re-install with ${INSTALL_SCRIPT}."
            exit 1
        fi
    else
        git config --global --add safe.directory "${APP_HOME}" 2>/dev/null || true
        cd "${APP_HOME}"
        git remote set-url origin "${GIT_REPO}"
        export GIT_TERMINAL_PROMPT=0
        # 无人值守加固 ①：pre-warm known_hosts (see do_install) — a fresh root env stalls git fetch on host-key verification.
        ensure_git_auth
        _fetch_ok=0
        for _i in 1 2 3; do
            if timeout "${GIT_TIMEOUT}" git fetch origin "${GIT_BRANCH}" 2>&1; then
                _fetch_ok=1
                break
            fi
            echo -e "${WARN} Git fetch failed or timed out (attempt ${_i}/3) — retrying"
            sleep $((5 * _i))
        done
        if [ "${_fetch_ok}" != "1" ]; then
            echo -e "${FAIL} Git fetch failed after 3 attempts (${GIT_TIMEOUT}s timeout each) — aborting"
            echo -e "${INFO} Check origin remote: git -C ${APP_HOME} remote -v"
            echo -e "${INFO} If it points to a mirror (ghfast.top/ghproxy), reset it:"
            echo -e "${INFO}   git -C ${APP_HOME} remote set-url origin ${GIT_REPO}"
            exit 1
        fi
        git merge "origin/${GIT_BRANCH}" --ff-only 2>/dev/null || {
            echo -e "${WARN} Fast-forward merge failed, falling back to reset"
            git reset --hard "origin/${GIT_BRANCH}" || {
                echo -e "${FAIL} Git reset failed."
                exit 1
            }
        }
    fi
    # Apply the sparse-checkout whitelist ONLY when SPARSE_DIRS is non-empty.
    # 根治 2026-08-20：同 do_install——SPARSE_DIRS 为空 = 全量检出，只执行 disable，绝不 set，
    # 杜绝 git pull 按旧白名单删除 cone 外的被跟踪文件。
    if [ -n "${SPARSE_DIRS:-}" ]; then
        # first disable to clear manual-mode leftovers, then init --cone + set;
        # on failure fall back to a full checkout. See the 审计 H6 note in do_install for details.
        git -C "${APP_HOME}" sparse-checkout disable 2>/dev/null || true
        if git -C "${APP_HOME}" sparse-checkout init --cone 2>/dev/null \
            && git -C "${APP_HOME}" sparse-checkout set ${SPARSE_DIRS} 2>/dev/null; then
            :
        else
            git -C "${APP_HOME}" sparse-checkout disable 2>/dev/null || true
            echo -e "${WARN} sparse-checkout failed — keeping full working tree"
        fi
    else
        git -C "${APP_HOME}" sparse-checkout disable 2>/dev/null || true
        echo -e "${INFO} SPARSE_DIRS empty — full checkout, sparse-checkout disabled"
    fi
    # No-domain scripts (install-local/code/dev) are kept on disk: deleting
    # git-tracked files leaves unstaged changes that break `git pull` / update.
    local after_commit
    after_commit=$(git log --oneline -1)
    done_step "Code updated: ${before_commit:0:7} -> ${after_commit:0:7}"

    # Self-update: if the entry script itself changed, re-run update with new version
    local script_md5
    script_md5=$(md5sum "${APP_HOME}/deploy/${INSTALL_SCRIPT}" | awk '{print $1}')
    if [ "${UPDATE_MD5}" != "${script_md5}" ]; then
        echo -e "${INFO} ${INSTALL_SCRIPT} updated, re-running with new version..."
        exec sudo APP_USER="${APP_USER}" APP_HOME="${APP_HOME}" VENV_DIR="${VENV_DIR}" REGION="${REGION}" FORCE_UPDATE="${FORCE_UPDATE:-0}" AUTO_DIR_CONFLICT="${AUTO_DIR_CONFLICT:-}" bash "${APP_HOME}/deploy/${INSTALL_SCRIPT}" update
        exit
    fi

    step "Update .env (fill missing keys)"
    update_env
    done_step ".env synced"

    # 审计 R3-M2：verify the VeroGuard integrity manifest after a code update (signed, no local regeneration)
    verify_release_manifest
    build_veroguard_manifest

    # Production gate: refuse to continue if DEBUG got enabled in .env (dev type exempt — debug on by design)
    if [ "${DEPLOY_TYPE}" != "dev" ]; then
        assert_debug_disabled
    fi

    step "Update Python dependencies"
    # 审计 P-1：automatically rebuilds when the venv is missing or its Python < 3.12 (compatible with upgrades from older 3.10 environments)
    _ensure_venv || {
        echo -e "${FAIL} Python venv setup failed — fix the error above and re-run."
        exit 1
    }
    # 审计 M16：prefer the hash-locked requirements.lock, fall back to requirements.txt when missing
    local _req_file="${APP_HOME}/requirements.txt"
    [ -f "${APP_HOME}/requirements.lock" ] && _req_file="${APP_HOME}/requirements.lock"
    req_hash=$(md5sum "${_req_file}" | awk '{print $1}')
    cached_hash=$(cat "${APP_HOME}/.requirements_hash" 2>/dev/null || echo "")
    if [ "${req_hash}" != "${cached_hash}" ]; then
        _pip_install -r "${_req_file}"
        echo "${req_hash}" > "${APP_HOME}/.requirements_hash"
    else
        echo -e "${INFO} ${_req_file} unchanged, skipping pip install"
    fi
    done_step "Dependencies updated"

    step "Update systemd services"
    chmod +x "${APP_HOME}/deploy/health_check.sh" 2>/dev/null || true
    write_systemd_services
    done_step "Systemd services updated"

    step "Update sudoers (one-click update permissions)"
    write_sudoers
    done_step "Sudoers updated"

    step "Update Nginx config"
    write_nginx_config
    nginx -t && systemctl restart nginx
    done_step "Nginx config updated"

    step "Pre-flight check"
    # Verify the database is reachable (connect directly with psycopg2, independent of the plugins package)
    _db_preflight() {
        sudo -u "${APP_USER}" bash -c "set -a; source ${APP_HOME}/.env; ${VENV_DIR}/bin/python -c \"
import os, psycopg2
conn = psycopg2.connect(
    host=os.getenv('PG_HOST', 'localhost'),
    port=os.getenv('PG_PORT', '5432'),
    dbname=os.getenv('PG_DB', 'appdb'),
    user=os.getenv('PG_USER', 'app'),
    password=os.getenv('PG_PASSWORD', ''),
)
conn.close()
print('DB OK')
\""
    }
    if _db_preflight; then
        :
    else
        # 审计 M8 fix：a historically frequent issue is the .env password not matching the actual DB password
        # (password authentication failed). Read PG_PASSWORD from .env and automatically
        # ALTER ROLE to sync the DB password, then retry (idempotent self-healing; only errors out on failure).
        echo -e "${WARN} Database connection failed — attempting to sync role password from .env"
        local _pg_user _pg_db _pg_pwd
        _pg_user=$(sudo -u "${APP_USER}" bash -c "source ${APP_HOME}/.env; echo \${PG_USER:-app}")
        _pg_db=$(sudo -u "${APP_USER}" bash -c "source ${APP_HOME}/.env; echo \${PG_DB:-appdb}")
        _pg_pwd=$(sudo -u "${APP_USER}" bash -c "source ${APP_HOME}/.env; echo \${PG_PASSWORD:-}")
        if [ -z "${_pg_pwd}" ]; then
            echo -e "${FAIL} PG_PASSWORD missing from ${APP_HOME}/.env"
            exit 1
        fi
        local _sql_esc="${_pg_pwd//\'/\'\'}"
        if printf "ALTER ROLE %s WITH LOGIN PASSWORD '%s';\n" "${_pg_user}" "${_sql_esc}" \
            | sudo -u postgres psql -q 2>&1; then
            echo -e "${OK} Role ${_pg_user} password synced from .env"
        else
            echo -e "${FAIL} Could not ALTER ROLE ${_pg_user} — check postgres access"
            exit 1
        fi
        if _db_preflight; then
            :
        else
            echo -e "${FAIL} Database still not accessible after password sync — aborting update"
            exit 1
        fi
    fi
    # Verify the Python syntax has no fatal errors
    if ! sudo -u "${APP_USER}" bash -c "${VENV_DIR}/bin/python -m py_compile ${APP_HOME}/admin/app.py"; then
        echo -e "${FAIL} Syntax error in new code — aborting update"
        exit 1
    fi
    done_step "Pre-flight passed"

    step "Restart services"
    restart_services
    done_step "Services restarted"

    step "Health check"
    health_check

    # ── Write final update status for admin UI polling ──
    trap - EXIT  # Clear the failure trap before writing success
    local _status_file="/run/verorun/update_status.json"
    mkdir -p /run/verorun 2>/dev/null || true
    chown "${APP_USER}:${APP_USER}" /run/verorun 2>/dev/null || true
    if [ "${UPDATE_FAILED:-0}" -eq 0 ]; then
        echo '{"status":"success","progress":100,"message":"Update completed successfully","error":null}' > "${_status_file}"
    else
        echo '{"status":"failed","progress":100,"message":"Update completed with errors","error":"Some services are unhealthy"}' > "${_status_file}"
    fi
}

# ── Summary: rendered conditionally by DEPLOY_TYPE ──
print_summary() {
    local PUBLIC_IP
    case "${DEPLOY_TYPE}" in
        production)
            PUBLIC_IP=$(curl -s --connect-timeout 5 --max-time 10 ifconfig.me 2>/dev/null || echo "unknown")

            echo ""
            echo "  ╔══════════════════════════════════════════════════════════════╗"
            echo "  ║              Deployment Complete!                             ║"
            if [ -n "${DOMAIN}" ]; then
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            # 审计 P0-1：summary reflects the real TLS state — https only when a certificate actually exists,
            # otherwise http + a prominent warning (DEPLOY_PROTOCOL may have been rewritten back to http).
            if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
            echo "  ║  Main site:  https://${DOMAIN}                                 ║"
            echo "  ║  Platform:   https://platform.${DOMAIN}                        ║"
            echo "  ║  Admin:      https://agent.${DOMAIN}/admin/                    ║"
            else
            echo "  ║  Main site:  http://${DOMAIN}                                  ║"
            echo "  ║  Platform:   http://platform.${DOMAIN}                         ║"
            echo "  ║  Admin:      http://agent.${DOMAIN}/admin/                     ║"
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  WARNING: no TLS certificate issued — running over HTTP.      ║"
            echo "  ║  SSO cookies lack the Secure flag; enable HTTPS via:          ║"
            echo "  ║    sudo certbot --nginx -d ${DOMAIN} -d www.${DOMAIN}         ║"
            echo "  ║             -d platform.${DOMAIN} -d agent.${DOMAIN}          ║"
            fi
            fi
            if [ "${APPROVE_MIGRATE:-0}" != "1" ]; then
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  WARNING: Admin account NOT created — admin panel inaccessible"
            echo "  ║  To fix: sudo bash deploy/${INSTALL_SCRIPT} seed                      ║"
            fi
            # 审计 R3-M3：show the admin credentials
            if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Admin login: ${VR_ADMIN_USERNAME:-administrator} / ***HIDDEN***"
            fi
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Useful commands:                                            ║"
            echo "  ║    systemctl status verorun-{main,auth,admin,guardian}       ║"
            echo "  ║    journalctl -u verorun-guardian -f                         ║"
            echo "  ║    bash deploy/${INSTALL_SCRIPT} update                              ║"
            echo "  ║    bash deploy/${INSTALL_SCRIPT} rollback                            ║"
            echo "  ╚══════════════════════════════════════════════════════════════╝"
            echo ""
            ;;
        lan)
            echo ""
            echo "  ╔══════════════════════════════════════════════════════════════╗"
            echo "  ║         No-domain / LAN Deployment Complete!                  ║"
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Main site:   http://localhost/                               ║"
            echo "  ║  Admin:       http://localhost/admin/                         ║"
            echo "  ║  Login:       http://localhost/login                          ║"
            PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
            if [ -n "${PUBLIC_IP}" ]; then
            echo "  ║  LAN access:  http://${PUBLIC_IP}/  (same paths)              ║"
            fi
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Useful commands:                                            ║"
            echo "  ║    systemctl status verorun-{main,auth,admin,guardian}       ║"
            echo "  ║    bash deploy/${INSTALL_SCRIPT} update                        ║"
            echo "  ║  SMS codes print to console until provider set (Admin→SMS)  ║"
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  AI API keys are empty by default — set real values in:      ║"
            echo "  ║    ${APP_HOME}/.env  (DASHSCOPE_TEXT_KEY / OPENAI_API_KEY /   ║"
            echo "  ║    DEEPSEEK_API_KEY) before enabling AI features             ║"
            if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Admin login: ${VR_ADMIN_USERNAME:-administrator} / ***HIDDEN***"
            fi
            echo "  ╚══════════════════════════════════════════════════════════════╝"
            echo ""
            ;;
        code)
            PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
            echo ""
            echo "  ╔══════════════════════════════════════════════════════════════╗"
            echo "  ║      Team Intranet Deployment Complete (Full Plugins)!        ║"
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Main site:   http://localhost/                               ║"
            echo "  ║  Admin:       http://localhost/admin/                         ║"
            echo "  ║  Console:     http://localhost/auth/                          ║"
            if [ -n "${PUBLIC_IP}" ]; then
            echo "  ║  LAN access:  http://${PUBLIC_IP}/  (same paths)              ║"
            fi
            echo "  ║  Plugins:     $(ls -d ${APP_HOME}/plugins/*/ 2>/dev/null | wc -l) directories installed                    ║"
            echo "  ║  Code size:   $(du -sh ${APP_HOME} 2>/dev/null | cut -f1)                              ║"
            # 审计 R3-M3：show the admin credentials
            if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Admin login: ${VR_ADMIN_USERNAME:-administrator} / ***HIDDEN***"
            fi
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Useful commands:                                            ║"
            echo "  ║    systemctl status verorun-{main,auth,admin,guardian}       ║"
            echo "  ║    bash deploy/${INSTALL_SCRIPT} update                         ║"
            echo "  ╚══════════════════════════════════════════════════════════════╝"
            echo ""
            ;;
        edu)
            PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
            echo ""
            echo "  +----------------------------------------------+"
            echo "  |     Educational Deployment Complete!         |"
            echo "  +----------------------------------------------+"
            echo "  -> Main site:   http://localhost/"
            echo "  -> Admin:       http://localhost/admin/"
            echo "  -> Console:     http://localhost/auth/"
            if [ -n "${PUBLIC_IP}" ]; then
            echo "  -> LAN access:  http://${PUBLIC_IP}/  (same paths)"
            fi
            echo "  -> Edu license: ${EDU_CODE:-NOT SET}"
            if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
            echo "  ==============================================="
            echo "  -> Admin login: ${VR_ADMIN_USERNAME:-administrator} / ***HIDDEN***"
            fi
            echo "  ==============================================="
            echo "  -> Useful commands:"
            echo "    systemctl status verorun-{main,auth,admin,guardian}"
            echo "    bash deploy/${INSTALL_SCRIPT} update"
            echo "  +----------------------------------------------+"
            echo ""
            ;;
        dev)
            echo ""
            echo "  ╔══════════════════════════════════════════════════════════════╗"
            echo "  ║         Developer Deployment Complete!                        ║"
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Main site:   http://localhost/                               ║"
            echo "  ║  Admin:       http://localhost/admin/                         ║"
            echo "  ║  Console:     http://localhost/auth/                          ║"
            PUBLIC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
            if [ -n "${PUBLIC_IP}" ]; then
            echo "  ║  LAN access:  http://${PUBLIC_IP}/  (same paths)              ║"
            fi
            echo "  ║  Plugins:     NOT installed (install via Admin panel)          ║"
            # 审计 R3-M3：show the admin credentials
            if [ "${APPROVE_MIGRATE:-0}" = "1" ]; then
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Admin login: ${VR_ADMIN_USERNAME:-administrator} / ***HIDDEN***"
            fi
            echo "  ╠══════════════════════════════════════════════════════════════╣"
            echo "  ║  Useful commands:                                            ║"
            echo "  ║    systemctl status verorun-{main,auth,admin,guardian}       ║"
            echo "  ║    bash deploy/${INSTALL_SCRIPT} update                          ║"
            echo "  ╚══════════════════════════════════════════════════════════════╝"
            echo ""
            ;;
        *)
            echo -e "${WARN} print_summary: unknown DEPLOY_TYPE '${DEPLOY_TYPE}'"
            ;;
    esac
}
