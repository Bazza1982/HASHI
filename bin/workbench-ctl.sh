#!/usr/bin/env bash
set -euo pipefail

# Navigate to project root (parent of bin/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKBENCH_DIR="$SCRIPT_DIR/workbench"
ECOSYSTEM="$WORKBENCH_DIR/ecosystem.config.cjs"
ACTION="${1:-start}"

SERVER_PORT=${HASHI_SERVER_PORT:-3001}
CLIENT_PORT=${HASHI_CLIENT_PORT:-5173}

health(){ curl -sf --max-time 3 "$1" >/dev/null 2>&1; }

[[ -d "$WORKBENCH_DIR/node_modules" ]] || (cd "$WORKBENCH_DIR" && npm install)

OPEN_BROWSER=0
if [[ "${2:-}" == "--open" || "${1:-}" == "--open" ]]; then
  OPEN_BROWSER=1
fi

case "$ACTION" in
  start)
    cd "$WORKBENCH_DIR"
    npx pm2 start "$ECOSYSTEM"
    echo "Waiting for services..."
    for _ in {1..30}; do
      sleep 1
      if health "http://localhost:$SERVER_PORT/api/config" && health "http://localhost:$CLIENT_PORT/"; then
        echo "Workbench started successfully."
        if [[ "$OPEN_BROWSER" == "1" ]]; then
          # Give it 15 seconds for the terminal animation and initial LLM hatching
          echo "Services ready. Allowing terminal animation and LLM hatching to complete (15s)..."
          sleep 15
          echo "Opening browser..."
          case "$(uname)" in
            Darwin)  open "http://localhost:$CLIENT_PORT" ;;
            Linux)   xdg-open "http://localhost:$CLIENT_PORT" ;;
            *)       powershell.exe -NoProfile -Command "Start-Process http://localhost:$CLIENT_PORT/" 2>/dev/null || true ;;
          esac
        fi
        npx pm2 list
        exit 0
      fi
    done
    echo "WARNING: Services started but health check not passing yet. Check: npx pm2 logs"
    npx pm2 list
    ;;
  stop)
    cd "$WORKBENCH_DIR"
    npx pm2 stop "$ECOSYSTEM" 2>/dev/null || true
    npx pm2 delete "$ECOSYSTEM" 2>/dev/null || true
    echo "Workbench stopped."
    ;;
  restart)
    cd "$WORKBENCH_DIR"
    npx pm2 restart "$ECOSYSTEM"
    echo "Workbench restarted."
    npx pm2 list
    ;;
  status)
    cd "$WORKBENCH_DIR"
    npx pm2 list
    if health "http://localhost:$SERVER_PORT/api/config"; then
      echo "Backend: healthy"
    else
      echo "Backend: not responding"
    fi
    if health "http://localhost:$CLIENT_PORT/"; then
      echo "Frontend: healthy"
    else
      echo "Frontend: not responding"
    fi
    ;;
  *) echo "Usage: $0 {start|stop|restart|status}"; exit 1;;
esac
