#!/usr/bin/env bash
#
# Kill all Bridge-U-F sessions
# Equivalent to kill_bridge_u_f_sessions.bat
#

set -euo pipefail

# Navigate to project root (parent of bin/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
BRIDGE_HOME="${BRIDGE_HOME:-$SCRIPT_DIR}"
PYTHON_EXE="${PYTHON_EXE:-python3}"
PID_FILE="$("$PYTHON_EXE" "$SCRIPT_DIR/scripts/resolve_instance_runtime.py" \
    --code-root "$SCRIPT_DIR" --bridge-home "$BRIDGE_HOME" --field pid-path)"
INSTANCE_ID="$("$PYTHON_EXE" "$SCRIPT_DIR/scripts/resolve_instance_runtime.py" \
    --code-root "$SCRIPT_DIR" --bridge-home "$BRIDGE_HOME" --field instance-id)"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

log() {
    if [[ "$QUIET" == "0" ]]; then
        echo "$@"
    fi
    return 0
}

log "================================================================"
log "           KILL BRIDGE-U-F REMAINING SESSIONS"
log "================================================================"
log ""

# Stop only the process recorded for this configured instance. Never scan
# global process names or shared default ports: those can belong to another
# HASHI instance on the same computer.
FOUND_ANY=0
PID=""

cmdline_matches_bridge_home() {
    local cmdline="$1"
    [[ "$cmdline" == *"main.py --bridge-home $BRIDGE_HOME "* ||
       "$cmdline" == *"main.py --bridge-home $BRIDGE_HOME" ]]
}

if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    CMDLINE=$(ps -p "$PID" -o args= 2>/dev/null || true)
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null &&
       cmdline_matches_bridge_home "$CMDLINE"; then
        log "Stopping $INSTANCE_ID (PID $PID)..."
        pkill -TERM -P "$PID" 2>/dev/null || true
        kill -TERM "$PID" 2>/dev/null || true
        FOUND_ANY=1
        sleep 1
    elif [[ -n "$PID" ]]; then
        log "Ignoring stale or foreign PID $PID for $INSTANCE_ID."
    fi
fi

if [[ "$FOUND_ANY" == "0" ]]; then
    log "No running process found for $INSTANCE_ID."
else
    shutdown_timeout="${HASHI_SHUTDOWN_TIMEOUT_S:-20}"
    waited=0
    while kill -0 "$PID" 2>/dev/null && [[ "$waited" -lt "$shutdown_timeout" ]]; do
        state=$(ps -p "$PID" -o stat= 2>/dev/null || true)
        if [[ "$state" == Z* ]]; then
            break
        fi
        sleep 1
        ((waited++)) || true
    done
    state=$(ps -p "$PID" -o stat= 2>/dev/null || true)
    if kill -0 "$PID" 2>/dev/null && [[ "$state" != Z* ]]; then
        log "Graceful shutdown timed out after ${shutdown_timeout}s; forcing PID $PID."
        pkill -KILL -P "$PID" 2>/dev/null || true
        kill -KILL "$PID" 2>/dev/null || true
    fi
    log "Cleanup commands issued."
fi

# Lock files are persistent by design; the OS lock is authoritative.
rm -f "$PID_FILE" 2>/dev/null || true
log "Removed $INSTANCE_ID PID file."

log ""
log "Cleanup complete."
