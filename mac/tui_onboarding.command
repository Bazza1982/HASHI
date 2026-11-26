#!/bin/bash
# ============================================================
# HASHI - TUI Onboarding (macOS)
# First-run setup: language, disclaimer, API key check,
# then seamless chat with Hashiko for Telegram + agent setup.
# Double-click this file in Finder to launch.
#
# Onboarding is part of the rescue hatch: it needs only
# textual / rich / aiohttp and never blocks on the full server
# dependency contract. Interpreters are tried in this order:
#   1. installer-qualified Functions runtime
#   2. portable private runtime  ./python
#   3. developer venv            ./.venv
#   4. a python3 on PATH
# Logs written to: mac/logs/tui_onboarding_<timestamp>.log
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/tui_onboarding_$(date '+%Y-%m-%d_%H%M%S').log"

# ---- Bootstrap interpreter (may lack the TUI packages; stdlib only) ----
BOOTSTRAP=""
if [ -x "$ROOT/python/bin/python3" ]; then
    BOOTSTRAP="$ROOT/python/bin/python3"
elif [ -x "$ROOT/.venv/bin/python3" ]; then
    BOOTSTRAP="$ROOT/.venv/bin/python3"
elif command -v python3 &>/dev/null; then
    BOOTSTRAP="$(command -v python3)"
else
    osascript -e 'display alert "HASHI Error" message "Python 3 not found.\nPlease run mac/prepare_usb.sh first." as critical'
    exit 1
fi

# ---- TUI interpreter: first candidate that can import textual/rich/aiohttp ----
PYTHON_EXE="$("$BOOTSTRAP" -B "$ROOT/scripts/resolve_tui_python.py" --code-root "$ROOT" 2>/dev/null)"
if [ -z "$PYTHON_EXE" ] || [ ! -x "$PYTHON_EXE" ]; then
    PYTHON_EXE="$BOOTSTRAP"
    echo "Notice: no interpreter with the TUI packages was found;"
    echo "        starting in rescue mode."
fi

# ---- Server runtime contract: informational only, never fatal ----
HASHI_TUI_RUNTIME_STATUS="unknown"
if "$PYTHON_EXE" -B "$ROOT/scripts/check_runtime_contract.py" --code-root "$ROOT" >> "$LOG_FILE" 2>&1; then
    HASHI_TUI_RUNTIME_STATUS="ok"
else
    HASHI_TUI_RUNTIME_STATUS="drifted"
    echo "NOTICE: Core/Functions server dependencies are not satisfied; onboarding starts anyway." >> "$LOG_FILE"
fi
export HASHI_TUI_RUNTIME_STATUS

# Bridge launch command (needs the qualified Core runtime when declared).
CORE_PYTHON="$PYTHON_EXE"
MAIN_SCRIPT="$ROOT/main.py"
MAIN_OPTIONS=()
if [ -f "$ROOT/constraints/core-host-py312.lock" ]; then
    QUALIFIED="$("$BOOTSTRAP" -I -B "$ROOT/scripts/resolve_launch_runtime.py" --code-root "$ROOT" --field functions-executable 2>/dev/null)"
    if [ -n "$QUALIFIED" ] && [ -x "$QUALIFIED" ]; then
        CORE_PYTHON="$QUALIFIED"
        MAIN_SCRIPT="$ROOT/scripts/run_qualified_core.py"
        MAIN_OPTIONS=(-I -B)
    else
        echo "Notice: the qualified Core runtime is unavailable; the bridge may not start." >> "$LOG_FILE"
    fi
fi
PID_FILE="$("$BOOTSTRAP" "$ROOT/scripts/resolve_instance_runtime.py" \
    --code-root "$ROOT" --bridge-home "$ROOT" --field pid-path 2>/dev/null)"
INSTANCE_ID="$("$BOOTSTRAP" "$ROOT/scripts/resolve_instance_runtime.py" \
    --code-root "$ROOT" --bridge-home "$ROOT" --field instance-id 2>/dev/null)"
[ -n "$INSTANCE_ID" ] || INSTANCE_ID="HASHI"

{
    echo "===================================================="
    echo "$INSTANCE_ID TUI Onboarding"
    echo "Started: $(date)"
    echo "Python:  $PYTHON_EXE"
    echo "Root:    $ROOT"
    echo "Server dependency contract: $HASHI_TUI_RUNTIME_STATUS"
    echo "===================================================="
} >> "$LOG_FILE"

echo "============================================================"
echo "  $INSTANCE_ID TUI Onboarding"
echo "  Log: $LOG_FILE"
echo "============================================================"
echo ""

# ---- Auto-start bridge if not running ----
BRIDGE_RUNNING=0
if [ -n "$PID_FILE" ] && [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        BRIDGE_RUNNING=1
    fi
fi

if [ "$BRIDGE_RUNNING" -eq 0 ]; then
    echo "Bridge not running - starting in background..."
    echo "Bridge not running - starting in background..." >> "$LOG_FILE"
    PYTHONPATH="$ROOT" "$CORE_PYTHON" "${MAIN_OPTIONS[@]}" "$MAIN_SCRIPT" \
        --bridge-home "$ROOT" \
        >> "$LOG_DIR/bridge_$(date '+%Y-%m-%d_%H%M%S').log" 2>&1 &
    echo "Waiting 15 seconds for bridge to initialise..."
    sleep 15
fi

# ---- Launch TUI Onboarding ----
echo "Starting TUI Onboarding..."
cd "$ROOT"
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH="$ROOT" "$PYTHON_EXE" -B tui_onboarding.py 2>> "$LOG_FILE"
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
