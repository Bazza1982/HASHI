#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
distro="${WSL_DISTRO_NAME:-}"

if [[ -z "$distro" ]]; then
  echo "Error: WSL_DISTRO_NAME is not set. Run this inside WSL." >&2
  exit 1
fi

powershell.exe -ExecutionPolicy Bypass -File "$(wslpath -w "$repo_root/tools/install_browser_option_d.ps1")" \
  -DistroName "$distro" \
  -LinuxRepoRoot "$repo_root"
