#!/bin/bash
# ============================================================
# HASHI9 - TUI Onboarding (macOS)
# First-run setup: language, disclaimer, API key check,
# then seamless chat with Hashiko for Telegram + agent setup.
# Double-click this file in Finder to launch.
# USB mode: uses portable Python from ./python/ if present.
# Fallback: uses system python3 or .venv.
# Logs written to: mac/logs/tui_onboarding_<timestamp>.log
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/tui_onboarding_$(date '+%Y-%m-%d_%H%M%S').log"
PID_FILE="$ROOT/.bridge_u_f.pid"

# ── Python detection ─────────────────────────────────────────
if [ -f "$ROOT/python/bin/python3" ]; then
    PYTHON_EXE="$ROOT/python/bin/python3"
elif [ -f "$ROOT/.venv/bin/python3" ]; then
    PYTHON_EXE="$ROOT/.venv/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON_EXE="$(command -v python3)"
else
    osascript -e 'display alert "HASHI9 Error" message "Python 3 not found.\nPlease run mac/prepare_usb.sh first." as critical'
    exit 1
fi

{
    echo "===================================================="
    echo "HASHI9 TUI Onboarding"
    echo "Started: $(date)"
    echo "Python:  $PYTHON_EXE"
    echo "Root:    $ROOT"
    echo "===================================================="
} >> "$LOG_FILE"

echo "============================================================"
echo "  HASHI9 TUI Onboarding"
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
        --workbench \
        >> "$LOG_DIR/bridge_$(date '+%Y-%m-%d_%H%M%S').log" 2>&1 &
    echo "Waiting 15 seconds for bridge to initialise..."
    sleep 15
fi

# ── Launch TUI Onboarding ────────────────────────────────────
echo "Starting TUI Onboarding..."
cd "$ROOT"
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH="$ROOT" "$PYTHON_EXE" tui_onboarding.py 2>> "$LOG_FILE"
EXIT_CODE=$?

{
    echo ""
    echo "Exit code: $EXIT_CODE"
    echo "Stopped: $(date)"
} >> "$LOG_FILE"

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "[ERROR] TUI Onboarding exited with error code $EXIT_CODE."
    echo "Log: $LOG_FILE"
fi

echo ""
echo "============================================================"
echo "  TUI Onboarding stopped. Exit code: $EXIT_CODE"
echo "  Log: $LOG_FILE"
echo "============================================================"
read -p "Press Enter to close..."
