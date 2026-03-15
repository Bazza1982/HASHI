#!/usr/bin/env bash
set -euo pipefail
# BIN_DIR is the directory containing this script
BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$BIN_DIR/kill-sessions.sh" --quiet || true
"$BIN_DIR/bridge-u.sh" "$@"
