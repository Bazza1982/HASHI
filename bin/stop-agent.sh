#!/usr/bin/env bash
set -euo pipefail
[[ -z "${1:-}" ]] && { echo "Usage: $0 agent-name"; exit 1; }
curl -s -X POST "http://127.0.0.1:18800/api/admin/stop-agent" -H "Content-Type: application/json" -d "{\"agent\":\"$1\"}"
