#!/usr/bin/env bash
set -euo pipefail
# BIN_DIR is the directory containing this script
BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$BIN_DIR/kill-sessions.sh" --quiet || true
bash "$BIN_DIR/bridge-u.sh" "$@"
