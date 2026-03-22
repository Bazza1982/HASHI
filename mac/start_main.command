#!/bin/bash
# ============================================================
# HASHI9 - Start Main Bridge (macOS)
# Double-click this file in Finder to launch the HASHI9 bridge.
# USB mode: uses portable Python from ./python/ if present.
# Logs written to: mac/logs/main_<timestamp>.log
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/main_$(date '+%Y-%m-%d_%H%M%S').log"

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
    echo "HASHI9 Main Bridge"
    echo "Started: $(date)"
    echo "Python:  $PYTHON_EXE"
    echo "Root:    $ROOT"
    echo "===================================================="
} | tee -a "$LOG_FILE"

echo ""
echo "Log: $LOG_FILE"
echo ""

cd "$ROOT"
PYTHONPATH="$ROOT" "$PYTHON_EXE" main.py --bridge-home "$ROOT" 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

{
    echo ""
    echo "Exit code: $EXIT_CODE"
    echo "Stopped: $(date)"
} | tee -a "$LOG_FILE"

echo ""
read -p "Press Enter to close..."
