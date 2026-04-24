# Option D Browser Bridge

This is the HASHI "real profile" bridge for Chrome on Windows:

- Chrome extension runs inside the user's real browser profile.
- Chrome native messaging launches a WSL-side host.
- HASHI agents in WSL talk to the host over a Unix socket.

## Why this exists

Chrome 136+ blocks `--remote-debugging-port` on the default profile. Chrome 144+ also adds per-connection approval for remote debugging against the real profile. This bridge avoids external CDP against the default profile.

## Architecture

Components:

- `tools/chrome_extension/hashi_browser_bridge`
  - Unpacked Chrome extension.
  - Uses `chrome.scripting` and `tabs` against the active real-profile browser session.
- `tools/browser_native_host.py`
  - Native messaging host launched by Chrome.
  - Exposes `/tmp/hashi-browser-bridge.sock` inside WSL for HASHI agents.
- `tools/browser_extension_bridge.py`
  - WSL client used by HASHI browser tools.
- `tools/install_browser_option_d.sh`
  - Installs the Windows native host manifest and copies the extension to `%LOCALAPPDATA%`.

## Install

From WSL:

```bash
cd /home/lily/projects/hashi
bash tools/install_browser_option_d.sh
```

Then in Windows Chrome:

1. Open `chrome://extensions`
2. Turn on `Developer mode`
3. Click `Load unpacked`
4. Select the printed extension directory under `%LOCALAPPDATA%\HASHI\browser_bridge\extension`

Expected extension id:

```text
jdeaedmoejdapldleofeggedgenogpka
```

## Runtime

When Chrome starts, the extension connects to the native host automatically. The native host creates:

```text
/tmp/hashi-browser-bridge.sock
```

HASHI browser tools auto-detect this bridge when:

- no explicit `cdp_url` is supplied
- `HASHI_BROWSER_BACKEND` is `auto` or `extension`

## Supported actions

Implemented:

- screenshot
- get_text
- get_html
- click
- fill
- evaluate
- scroll
- hover
- key
- select
- wait_for
- get_attribute
- active_tab
- session

Not yet implemented in the extension path:

- drag
- upload

These still require Playwright/CDP/standalone fallback.

## Logging

Host log:

```text
logs/browser_native_host.log
```

Structured browser audit log:

```text
logs/browser_action_audit.jsonl
```

Each record includes:

- timestamp
- action name
- request id
- session id
- sanitized args
- response summary
- elapsed time

This is intended for traceability and auditability across HASHI browser actions.

## Notes

- This design is intentionally modular: the WSL client only knows about a socket protocol.
- If we later replace native messaging with a different transport, the browser tool layer can stay stable.
- The core host is deliberately small so protocol and feature updates do not require redesigning the bridge.
