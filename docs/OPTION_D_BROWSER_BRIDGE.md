# Option D Browser Bridge

This is the HASHI "real profile" bridge for Chrome:

- Chrome extension runs inside the user's real browser profile.
- Chrome native messaging launches a local host process.
- HASHI agents use an authenticated Windows named pipe on native Windows or a Unix socket on Linux/WSL.

## Why this exists

Chrome 136+ blocks `--remote-debugging-port` on the default profile. Chrome 144+ also adds per-connection approval for remote debugging against the real profile. This bridge avoids external CDP against the default profile.

## Architecture

Components:

- `tools/chrome_extension/hashi_browser_bridge`
  - Unpacked Chrome extension.
  - Uses `chrome.scripting` and `tabs` against the active real-profile browser session.
- `tools/browser_native_host.py`
  - Native messaging host launched by Chrome.
  - Exposes `\\.\pipe\hashi-browser-bridge` on native Windows or `/tmp/hashi-browser-bridge.sock` on Linux/WSL.
- `tools/browser_extension_bridge.py`
  - Cross-platform client used by HASHI browser tools.
- `tools/install_browser_option_d_windows.ps1`
  - Installs the native Windows host and Chrome extension without WSL.
- `tools/install_browser_option_d.sh`
  - Installs the WSL-backed Windows host manifest and copies the extension to `%LOCALAPPDATA%`.
- `tools/install_browser_option_d_linux.sh`
  - Installs an isolated Linux native host manifest for Chrome running inside WSL/X11 and copies a WSL-specific extension bundle to `~/.local/share/hashi/browser_bridge_wsl/extension`.

## Install

From native Windows PowerShell for Windows Chrome:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\tools\install_browser_option_d_windows.ps1
```

The installer uses `.venv\Scripts\python.exe` when present, otherwise the `python.exe` on `PATH`.

From WSL for Windows Chrome (legacy WSL-backed transport):

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

When Chrome starts, the extension connects to the native host automatically. The native host creates one of these endpoints:

```text
Windows: \\.\pipe\hashi-browser-bridge
Linux/WSL: /tmp/hashi-browser-bridge.sock
```

The Windows pipe uses a per-user authentication key stored at:

```text
%LOCALAPPDATA%\HASHI\browser_bridge\bridge-auth.key
```

The Windows native-host log defaults to:

```text
%LOCALAPPDATA%\HASHI\browser_bridge\logs\native-host.log
```

The legacy WSL-backed installer explicitly keeps using:

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
- react  (verified Like reaction matched to a unique post excerpt and optional author)
- fill
- type_text  (CDP Input.insertText — for React/contenteditable editors; use instead of fill on LinkedIn etc.)
- evaluate
- scroll  (detects internal scroll containers and returns the target, coordinates, and `state_changed`)
- hover  (CDP `Input.dispatchMouseEvent` / `mouseMoved`; supports `timeout_ms`, `wait_ms`, `x_ratio`, `y_ratio` — see [BROWSER_BRIDGE_HOVER_NOTE.md](BROWSER_BRIDGE_HOVER_NOTE.md))
- active_tab
- media_state  (structured paused/time/duration/readiness state for the primary visible media element)
- media_play  (idempotent play with advancing-time verification; never a blind play/pause toggle)
- session  (executes supported steps sequentially and returns a result for every step)

Model-facing HASHI tools also expose `browser_active_tab`, `browser_get_media_state`,
`browser_play`, and the high-level `browser_open_play_verify` action. The high-level
action opens the selected visible page, maximizes the normal Chrome window, starts
media idempotently, and returns success only after URL/title, maximize state, and
advancing playback time are all verified.

Extension package version: **0.2.0** (`tools/chrome_extension/hashi_browser_bridge/`).

Not yet implemented in the extension path:

- key
- select
- wait_for
- get_attribute
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

- This design is intentionally modular: browser tools use the same JSON protocol over the platform transport.
- If we later replace native messaging with a different transport, the browser tool layer can stay stable.
- The core host is deliberately small so protocol and feature updates do not require redesigning the bridge.
