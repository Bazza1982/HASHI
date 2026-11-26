#!/bin/bash
# ============================================================
# HASHI - Start TUI (macOS)
# Double-click this file in Finder to launch this instance's TUI.
# Auto-starts the main bridge if not already running.
#
# The TUI is the rescue hatch. It only needs textual / rich / aiohttp,
# so this script never blocks on the full Core/Functions dependency
# contract. Interpreters are tried in this order:
#   1. installer-qualified Functions runtime
#   2. portable private runtime  ./python
#   3. developer venv            ./.venv
#   4. a python3 on PATH
# Server dependency drift is reported inside the TUI (/doctor), never by
# refusing to start it.
# Logs written to: mac/logs/tui_<timestamp>.log
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -x "$ROOT/media/transcription/bin/python3" ]; then
    export HASHI_TRANSCRIPTION_PYTHON="$ROOT/media/transcription/bin/python3"
    export HASHI_TTS_PYTHON="$ROOT/media/tts/bin/python3"
    export HF_HUB_CACHE="$ROOT/media/model-cache"
    export HASHI_MEDIA_FFMPEG="$("$HASHI_TTS_PYTHON" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
fi
LOG_DIR="$(dirname "$0")/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/tui_$(date '+%Y-%m-%d_%H%M%S').log"

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
    echo "        starting in rescue mode. Install them or use /doctor inside the TUI."
fi

# ---- Server runtime: reported by /doctor, never by this launcher ----
# The TUI does not use server-side packages, so running the server dependency
# contract here would check the wrong role under the TUI interpreter and blame
# the TUI for a server mismatch. /doctor reports the Core/Functions pair from
# the qualified selector instead.

# Bridge launch command (needs the qualified Core runtime when declared).
# Note: the bridge and the TUI may use different interpreters - the TUI must
# never be downgraded to an interpreter that lacks its own three packages.
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

# ---- Launch TUI ----
echo "Starting TUI..."
cd "$ROOT"
PYTHONPATH="$ROOT" "$PYTHON_EXE" -B tui.py 2>> "$LOG_FILE"
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
