#!/bin/bash
# VeroRun resource watchdog — standalone, runs via crontab every minute.
# =========================================================================
# Purpose: keep running (and leave a trace) even when the whole app stack
# freezes due to OOM — in-app health schedulers die together with the apps.
#
# Checks (thresholds overridable via env):
#   1. Available memory < 100MB                       -> MEM_WARN
#   2. Swap usage        >= 80%                       -> SWAP_CRIT
#   3. Key process count < 4 or > 25                  -> PROC_ABNORMAL
#
# Writes timestamped lines to ${VR_MONITOR_LOG} and touches a heartbeat file.
# NOTE: script runs as non-root cron, so default log dir must be world-writable
# (/var/tmp persists across reboot, unlike /tmp).
# Idempotent; no side effects; safe to run manually any time.
# =========================================================================

LOG_FILE="${VR_MONITOR_LOG:-/var/tmp/verorun-monitor.log}"
HEARTBEAT="${VR_MONITOR_HEARTBEAT:-/var/tmp/verorun_monitor_alive}"
MEM_WARN_MB="${VR_MONITOR_MEM_WARN_MB:-100}"
SWAP_CRIT_PCT="${VR_MONITOR_SWAP_CRIT_PCT:-80}"
PROC_MIN="${VR_MONITOR_PROC_MIN:-4}"
PROC_MAX="${VR_MONITOR_PROC_MAX:-25}"

TS="$(date '+%Y-%m-%d %H:%M:%S')"

# 每次运行更新心跳（最后一刻也能留痕）
touch "$HEARTBEAT"

# --- 1. Available memory (MB) ---
mem_available="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null)"
mem_available="${mem_available:-9999}"

# --- 2. Swap usage (%) ---
read -r st sf <<< "$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print t, f}' /proc/meminfo 2>/dev/null)"
swap_used=0
if [ "${st:-0}" -gt 0 ]; then
    swap_used=$(( (st - sf) * 100 / st ))
fi

# --- 3. Key process count ---
proc_count="$(pgrep -fc 'gunicorn|run_gunicorn|guardian' 2>/dev/null || echo 0)"

msg=""
if [ "$mem_available" -lt "$MEM_WARN_MB" ]; then
    msg="MEM_WARN available=${mem_available}MB < ${MEM_WARN_MB}MB"
fi
if [ "$swap_used" -ge "$SWAP_CRIT_PCT" ]; then
    if [ -n "$msg" ]; then msg="${msg}; "; fi
    msg="${msg}SWAP_CRIT used=${swap_used}% >= ${SWAP_CRIT_PCT}%"
fi
if [ "$proc_count" -lt "$PROC_MIN" ] || [ "$proc_count" -gt "$PROC_MAX" ]; then
    if [ -n "$msg" ]; then msg="${msg}; "; fi
    msg="${msg}PROC_ABNORMAL count=${proc_count} (range ${PROC_MIN}-${PROC_MAX})"
fi

if [ -n "$msg" ]; then
    echo "[$TS] ALERT $msg" >> "$LOG_FILE"
else
    echo "[$TS] OK mem=${mem_available}MB swap=${swap_used}% proc=${proc_count}" >> "$LOG_FILE"
fi

exit 0
