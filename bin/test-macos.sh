#!/usr/bin/env bash
# bin/test-macos.sh — Quick sanity check for macOS compatibility

set -euo pipefail
PASS=0; FAIL=0

check() {
    local name="$1"; shift
    if "$@" &>/dev/null; then
        echo "✅  $name"; ((PASS++))
    else
        echo "❌  $name"; ((FAIL++))
    fi
}

check "python3 available"    python3 --version
check "node available"       node --version
check "ffmpeg available"     ffmpeg -version
check "say available"        which say
check "pip available"        pip3 --version
check "telegram-bot installed" python3 -c "import telegram"
check "httpx installed"      python3 -c "import httpx"
check "aiohttp installed"    python3 -c "import aiohttp"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1