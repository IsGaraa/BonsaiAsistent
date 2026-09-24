#!/usr/bin/env bash
# BONSAI - Linux stopper (mirrors stop.cmd): kills the model server (8080)
# and the web UI (8081).
cd "$(dirname "$0")"

stopped=0
for pat in "llama-server" "bonsai_web.py"; do
    pids="$(pgrep -f "$pat" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "Stopping '$pat' (PID $pids)."
        echo "$pids" | xargs -r kill 2>/dev/null || true
        stopped=1
    fi
done
if [ "$stopped" -eq 0 ]; then
    echo "Bonsai is not running."
fi