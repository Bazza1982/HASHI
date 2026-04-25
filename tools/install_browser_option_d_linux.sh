#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host_name="${HASHI_BROWSER_BRIDGE_HOST_NAME:-com.hashi.browser_bridge.wsl}"
extension_id="jdeaedmoejdapldleofeggedgenogpka"
socket_path="${HASHI_BROWSER_BRIDGE_SOCKET:-/tmp/hashi-browser-bridge-wsl.sock}"

install_root="${HASHI_BROWSER_BRIDGE_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/hashi/browser_bridge_wsl}"
extension_install_dir="$install_root/extension"
wrapper_path="$install_root/hashi_browser_bridge_host.sh"

chrome_config_home="${XDG_CONFIG_HOME:-$HOME/.config}/google-chrome"
manifest_dir="$chrome_config_home/NativeMessagingHosts"
manifest_path="$manifest_dir/$host_name.json"

mkdir -p "$install_root" "$manifest_dir"

python3 - <<'PY' "$repo_root" "$extension_install_dir" "$host_name"
from pathlib import Path
import shutil
import sys

from tools.browser_bridge_harness import build_extension_bundle

repo_root = Path(sys.argv[1])
target_dir = Path(sys.argv[2])
host_name = sys.argv[3]
source_dir = repo_root / "tools" / "chrome_extension" / "hashi_browser_bridge"

if target_dir.exists():
    shutil.rmtree(target_dir)
build_extension_bundle(
    source_dir,
    target_dir,
    host_name=host_name,
    extension_name="HASHI Browser Bridge WSL",
)
PY

cat > "$wrapper_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$repo_root"
exec /usr/bin/env python3 -m tools.browser_native_host --stdio --socket "$socket_path" --log-file "$repo_root/logs/browser_native_host.log"
EOF
chmod 0755 "$wrapper_path"

cat > "$manifest_path" <<EOF
{
  "name": "$host_name",
  "description": "HASHI Browser Bridge native host",
  "path": "$wrapper_path",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$extension_id/"
  ]
}
EOF

echo "[HASHI Option D Linux] Copied extension to: $extension_install_dir"
echo "[HASHI Option D Linux] Wrote wrapper: $wrapper_path"
echo "[HASHI Option D Linux] Wrote manifest: $manifest_path"
echo "[HASHI Option D Linux] Host name: $host_name"
echo "[HASHI Option D Linux] Socket path: $socket_path"
echo
echo "Install steps for Linux Chrome on WSL/X11:"
echo "1. Open Chrome on DISPLAY :10 and go to chrome://extensions"
echo "2. Enable Developer mode"
echo "3. Click 'Load unpacked'"
echo "4. Select: $extension_install_dir"
echo
echo "Expected extension ID: $extension_id"
