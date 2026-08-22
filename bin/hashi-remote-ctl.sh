#!/usr/bin/env bash
#
# Manage Hashi Remote as an OS-supervised side program on Linux/WSL.
#
# This script installs a systemd --user service when systemd is available.
# It keeps legacy `/remote on` untouched; supervised Remote is an optional
# rescue-grade lifecycle for machines that need remote recovery.

set -euo pipefail

ACTION="${1:-status}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HASHI_ROOT="${HASHI_ROOT:-$SCRIPT_DIR}"
if [[ ! -d "$HASHI_ROOT" ]]; then
    echo "HASHI root does not exist: $HASHI_ROOT" >&2
    exit 66
fi
HASHI_ROOT="$(cd "$HASHI_ROOT" && pwd -P)"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LOG_DIR="$HASHI_ROOT/logs"
LOG_PATH="$LOG_DIR/hashi-remote-supervisor.log"

if [[ -x "$HASHI_ROOT/.venv/bin/python3" ]]; then
    PYTHON_BIN="${HASHI_REMOTE_PYTHON:-$HASHI_ROOT/.venv/bin/python3}"
else
    PYTHON_BIN="${HASHI_REMOTE_PYTHON:-python3}"
fi

IDENTITY_SCRIPT="$HASHI_ROOT/remote/supervisor_identity.py"
if [[ ! -f "$IDENTITY_SCRIPT" ]]; then
    echo "Missing supervisor identity helper: $IDENTITY_SCRIPT" >&2
    exit 66
fi
IDENTITY_ARGS=(--hashi-root "$HASHI_ROOT" --format lines)
if [[ -n "${HASHI_INSTANCE_ID:-}" ]]; then
    IDENTITY_ARGS+=(--instance-id "$HASHI_INSTANCE_ID")
fi
mapfile -t SUPERVISOR_IDENTITY < <("$PYTHON_BIN" "$IDENTITY_SCRIPT" "${IDENTITY_ARGS[@]}")
if [[ "${#SUPERVISOR_IDENTITY[@]}" -lt 5 ]]; then
    echo "Could not resolve the Hashi Remote supervisor identity." >&2
    exit 70
fi
INSTANCE_ID="${SUPERVISOR_IDENTITY[0]}"
INSTANCE_SLUG="${SUPERVISOR_IDENTITY[1]}"
DEFAULT_SERVICE_NAME="${SUPERVISOR_IDENTITY[2]}"
IDENTITY_SOURCE="${SUPERVISOR_IDENTITY[4]}"
SERVICE_NAME="${HASHI_REMOTE_SERVICE_NAME:-$DEFAULT_SERVICE_NAME}"
if [[ ! "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@:-]+\.service$ ]]; then
    echo "Invalid systemd service name: $SERVICE_NAME" >&2
    exit 64
fi
SERVICE_PATH="$SYSTEMD_USER_DIR/$SERVICE_NAME"

REMOTE_ARGS=(--hashi-root "$HASHI_ROOT" --supervised)

if [[ "${HASHI_REMOTE_NO_TLS:-0}" == "1" ]]; then
    REMOTE_ARGS+=(--no-tls)
fi

if [[ -n "${HASHI_REMOTE_MAX_TERMINAL_LEVEL:-}" ]]; then
    REMOTE_ARGS+=(--max-terminal-level "$HASHI_REMOTE_MAX_TERMINAL_LEVEL")
fi

if [[ -n "${HASHI_REMOTE_DISCOVERY:-}" ]]; then
    REMOTE_ARGS+=(--discovery "$HASHI_REMOTE_DISCOVERY")
fi

if [[ -n "${HASHI_REMOTE_PORT:-}" ]]; then
    REMOTE_ARGS+=(--port "$HASHI_REMOTE_PORT")
fi

have_systemd_user() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1
}

unit_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

write_service() {
    mkdir -p "$SYSTEMD_USER_DIR" "$LOG_DIR"
    local temp_path working_directory instance_environment exec_start log_target arg
    temp_path="$(mktemp "$SYSTEMD_USER_DIR/.${SERVICE_NAME}.XXXXXX")"
    working_directory="$(unit_quote "$HASHI_ROOT")"
    instance_environment="$(unit_quote "HASHI_INSTANCE_ID=$INSTANCE_ID")"
    exec_start=""
    for arg in "$PYTHON_BIN" -m remote "${REMOTE_ARGS[@]}"; do
        [[ -z "$exec_start" ]] || exec_start+=" "
        exec_start+="$(unit_quote "$arg")"
    done
    log_target="$(unit_quote "append:$LOG_PATH")"
    cat > "$temp_path" <<EOF
[Unit]
Description=Hashi Remote side program ($INSTANCE_SLUG)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$working_directory
Environment=HASHI_REMOTE_SUPERVISED=1
Environment=$instance_environment
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=$exec_start
Restart=always
RestartSec=5
StandardOutput=$log_target
StandardError=$log_target

[Install]
WantedBy=default.target
EOF
    chmod 0644 "$temp_path"
    mv "$temp_path" "$SERVICE_PATH"
}

require_systemd_user() {
    if ! have_systemd_user; then
        echo "systemd --user is not available in this shell."
        echo "Use: $PYTHON_BIN -m remote ${REMOTE_ARGS[*]}"
        exit 2
    fi
}

case "$ACTION" in
    install)
        require_systemd_user
        write_service
        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME"
        echo "Installed $SERVICE_PATH"
        echo "Instance $INSTANCE_ID ($IDENTITY_SOURCE)"
        ;;
    uninstall)
        require_systemd_user
        systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
        rm -f "$SERVICE_PATH"
        systemctl --user daemon-reload
        echo "Uninstalled $SERVICE_NAME"
        ;;
    start)
        require_systemd_user
        [[ -f "$SERVICE_PATH" ]] || write_service
        systemctl --user daemon-reload
        systemctl --user start "$SERVICE_NAME"
        ;;
    stop)
        require_systemd_user
        systemctl --user stop "$SERVICE_NAME"
        ;;
    restart)
        require_systemd_user
        [[ -f "$SERVICE_PATH" ]] || write_service
        systemctl --user daemon-reload
        systemctl --user restart "$SERVICE_NAME"
        ;;
    status)
        echo "Instance: $INSTANCE_ID ($IDENTITY_SOURCE)"
        echo "Service: $SERVICE_NAME"
        echo "Root: $HASHI_ROOT"
        if have_systemd_user; then
            systemctl --user status "$SERVICE_NAME" --no-pager || true
        else
            echo "systemd --user unavailable"
            echo "Fallback command: $PYTHON_BIN -m remote ${REMOTE_ARGS[*]}"
            exit 2
        fi
        ;;
    logs)
        if have_systemd_user; then
            journalctl --user -u "$SERVICE_NAME" -n "${HASHI_REMOTE_LOG_LINES:-120}" --no-pager || true
        fi
        [[ -f "$LOG_PATH" ]] && tail -n "${HASHI_REMOTE_LOG_LINES:-120}" "$LOG_PATH"
        ;;
    command)
        echo "$PYTHON_BIN -m remote ${REMOTE_ARGS[*]}"
        ;;
    service-name)
        echo "$SERVICE_NAME"
        ;;
    *)
        echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs|command|service-name}"
        exit 64
        ;;
esac
