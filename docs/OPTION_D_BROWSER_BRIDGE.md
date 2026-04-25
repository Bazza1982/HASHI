# Option D Browser Bridge

This is the HASHI "real profile" bridge for Chrome:

- Chrome extension runs inside the user's real browser profile.
- Chrome native messaging launches a local host process.
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
- `tools/install_browser_option_d_linux.sh`
  - Installs an isolated Linux native host manifest for Chrome running inside WSL/X11 and copies a WSL-specific extension bundle to `~/.local/share/hashi/browser_bridge_wsl/extension`.

## Install

From WSL for Windows Chrome:

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

For Linux Chrome running inside WSL/X11:

```bash
cd /home/lily/projects/hashi
bash tools/install_browser_option_d_linux.sh
```

Then in Linux Chrome:

1. Open `chrome://extensions`
2. Turn on `Developer mode`
3. Click `Load unpacked`
4. Select the printed extension directory under `~/.local/share/hashi/browser_bridge_wsl/extension`

By default this Linux installer uses an isolated host/socket pair so it does not get stolen by a Windows Chrome instance that is already reconnecting:

- host name: `com.hashi.browser_bridge.wsl`
- socket: `/tmp/hashi-browser-bridge-wsl.sock`

When using the isolated WSL Chrome profile at `~/.config/google-chrome-wsl-bridge`, Chrome looks for native messaging manifests under that profile's own `NativeMessagingHosts` directory. The Linux installer now writes the manifest to both:

- `~/.config/google-chrome/NativeMessagingHosts`
- `~/.config/google-chrome-wsl-bridge/NativeMessagingHosts`

This avoids the failure mode where the extension loads and its service worker runs, but `connectNative("com.hashi.browser_bridge.wsl")` returns `Specified native messaging host not found.` because the manifest only exists under the default Chrome config path.

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

## Windows live notes

Known-good Windows live extension actions now include:

- active_tab
- get_text
- get_html
- screenshot
- click
- fill
- evaluate

Two practical Windows details matter:

1. Updating files on disk is not always enough for an unpacked extension that is already running.
2. On this host, the reliable way to force Chrome to pick up the new service worker code was:
   - focus the real Chrome window
   - open `chrome://extensions`
   - press `Tab` 11 times
   - press `Enter`

This is useful when a new extension action still returns `unsupported action: ...` even though the unpacked extension files on disk have already been updated.

Known-good live validation after the Windows action-surface upgrade:

- `fill` wrote `OpenAI` into the Wikipedia search input
- `evaluate('document.title')` returned `Wikipedia`
- `evaluate('JSON.stringify({title: document.title})')` returned serialized JSON
- `click('button[type="submit"]')` triggered navigation to Wikipedia search results

Desktop-control caveat:

- The visible Windows desktop may still show a different top window than the one receiving keyboard input.
- When desktop visuals disagree with the bridge, trust bridge-owned evidence first:
  - `/tmp/hashi-browser-bridge.sock`
  - `logs/browser_native_host.log`
  - direct bridge responses from `tools.browser_extension_bridge`
