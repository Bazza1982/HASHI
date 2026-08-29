#!/bin/bash
# ============================================================
# HASHI - Start TUI (macOS)
# Double-click this file in Finder to launch this instance's TUI.
# Auto-starts the main bridge if not already running.
# USB mode: uses portable Python from ./python/ if present.
# Fallback: uses system python3 or .venv.
# Logs written to: mac/logs/tui_<timestamp>.log
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/tui_$(date '+%Y-%m-%d_%H%M%S').log"

# ── Python detection ─────────────────────────────────────────
if [ -f "$ROOT/python/bin/python3" ]; then
    PYTHON_EXE="$ROOT/python/bin/python3"
elif [ -f "$ROOT/.venv/bin/python3" ]; then
    PYTHON_EXE="$ROOT/.venv/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON_EXE="$(command -v python3)"
else
    osascript -e 'display alert "HASHI Error" message "Python 3 not found.\nPlease run mac/prepare_usb.sh first." as critical'
    exit 1
fi
PID_FILE="$("$PYTHON_EXE" "$ROOT/scripts/resolve_instance_runtime.py" \
    --code-root "$ROOT" --bridge-home "$ROOT" --field pid-path)"
INSTANCE_ID="$("$PYTHON_EXE" "$ROOT/scripts/resolve_instance_runtime.py" \
    --code-root "$ROOT" --bridge-home "$ROOT" --field instance-id)"

{
    echo "===================================================="
    echo "$INSTANCE_ID TUI"
    echo "Started: $(date)"
    echo "Python:  $PYTHON_EXE"
    echo "Root:    $ROOT"
    echo "===================================================="
} >> "$LOG_FILE"

echo "============================================================"
echo "  $INSTANCE_ID TUI"
echo "  Log: $LOG_FILE"
echo "============================================================"
echo ""

# ── Auto-start bridge if not running ─────────────────────────
BRIDGE_RUNNING=0
if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        BRIDGE_RUNNING=1
    fi
fi

if [ "$BRIDGE_RUNNING" -eq 0 ]; then
    echo "Bridge not running — starting in background..."
    echo "Bridge not running — starting in background..." >> "$LOG_FILE"
    PYTHONPATH="$ROOT" "$PYTHON_EXE" "$ROOT/main.py" \
        --bridge-home "$ROOT" \
        >> "$LOG_DIR/bridge_$(date '+%Y-%m-%d_%H%M%S').log" 2>&1 &
    echo "Waiting 15 seconds for bridge to initialise..."
    sleep 15
fi

# ── Launch TUI ───────────────────────────────────────────────
echo "Starting TUI..."
cd "$ROOT"
PYTHONPATH="$ROOT" "$PYTHON_EXE" tui.py 2>> "$LOG_FILE"
EXIT_CODE=$?

{
    echo ""
    echo "Exit code: $EXIT_CODE"
    echo "Stopped: $(date)"
} >> "$LOG_FILE"

echo ""
echo "============================================================"
echo "  TUI stopped. Exit code: $EXIT_CODE"
echo "  Log: $LOG_FILE"
echo "============================================================"
read -p "Press Enter to close..."
