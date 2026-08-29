#!/bin/bash
# VeroRun service health check
# Usage: health_check.sh <port>
# Waits up to 180 seconds for the service on 127.0.0.1:<port>/health to return 200.
# Exits 0 on success, 1 on timeout.

PORT="${1:-8081}"
MAX_WAIT=180
INTERVAL=1

for i in $(seq 1 "${MAX_WAIT}"); do
    # -m 5 --connect-timeout 3: prevent curl from hanging forever during slow startup
    # (a hang here would block systemd start-post indefinitely)
    if curl -sf -m 5 --connect-timeout 3 "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
        exit 0
    fi
    sleep "${INTERVAL}"
done

exit 1