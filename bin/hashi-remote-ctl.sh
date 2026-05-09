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
SERVICE_NAME="${HASHI_REMOTE_SERVICE_NAME:-hashi-remote.service}"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_PATH="$SYSTEMD_USER_DIR/$SERVICE_NAME"
LOG_DIR="$HASHI_ROOT/logs"
LOG_PATH="$LOG_DIR/hashi-remote-supervisor.log"

if [[ -x "$HASHI_ROOT/.venv/bin/python3" ]]; then
    PYTHON_BIN="${HASHI_REMOTE_PYTHON:-$HASHI_ROOT/.venv/bin/python3}"
else
    PYTHON_BIN="${HASHI_REMOTE_PYTHON:-python3}"
fi

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

write_service() {
    mkdir -p "$SYSTEMD_USER_DIR" "$LOG_DIR"
    cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Hashi Remote side program
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HASHI_ROOT
Environment=HASHI_REMOTE_SUPERVISED=1
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=$PYTHON_BIN -m remote ${REMOTE_ARGS[*]}
Restart=always
RestartSec=5
StandardOutput=append:$LOG_PATH
StandardError=append:$LOG_PATH

[Install]
WantedBy=default.target
EOF
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
    *)
        echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs|command}"
        exit 64
        ;;
esac
