#!/usr/bin/env bash
#
# Kill all Bridge-U-F sessions
# Equivalent to kill_bridge_u_f_sessions.bat
#

set -euo pipefail

# Navigate to project root (parent of bin/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

log() { [[ "$QUIET" == "0" ]] && echo "$@"; }

log "================================================================"
log "           KILL BRIDGE-U-F REMAINING SESSIONS"
log "================================================================"
log ""

# Find and kill bridge-u-f Python processes
FOUND_ANY=0

# Method 1: Find by PID file
if [[ -f "$SCRIPT_DIR/.bridge_u_f.pid" ]]; then
    PID=$(cat "$SCRIPT_DIR/.bridge_u_f.pid" 2>/dev/null || echo "")
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        log "Stopping main process (PID $PID)..."
        kill -TERM "$PID" 2>/dev/null || true
        FOUND_ANY=1
        sleep 1
    fi
fi

# Method 2: Find by process pattern
while IFS= read -r pid; do
    if [[ -n "$pid" ]]; then
        FOUND_ANY=1
        log "Stopping PID $pid..."
        kill -TERM "$pid" 2>/dev/null || true
    fi
done < <(pgrep -f "python.*main\.py.*bridge" 2>/dev/null || true)

# Method 3: Check ports
for port in 18800 18801; do
    pid=$(lsof -ti :"$port" 2>/dev/null || true)
    if [[ -n "$pid" ]]; then
        FOUND_ANY=1
        log "Stopping process on port $port (PID $pid)..."
        kill -TERM "$pid" 2>/dev/null || true
    fi
done

if [[ "$FOUND_ANY" == "0" ]]; then
    log "No bridge-u-f processes found."
else
    sleep 2
    # Force kill any remaining
    pgrep -f "python.*main\.py.*bridge" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    log "Cleanup commands issued."
fi

# Clean up lock files
rm -f "$SCRIPT_DIR/.bridge_u_f.lock" "$SCRIPT_DIR/.bridge_u_f.pid" 2>/dev/null || true
log "Removed stale lock/pid files."

log ""
log "Cleanup complete."
