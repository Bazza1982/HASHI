# Windows Live Browser Bridge

This document is the practical runbook for using the HASHI Chrome control extension against the real Windows Chrome session.

## Scope

Use this when:

- Chrome is running on the real Windows desktop
- the extension is installed as an unpacked extension
- HASHI agents need to verify or recover the live bridge without relying on external CDP against the default profile

## Known-good bridge evidence

Prefer these checks over desktop appearance:

- socket: `/tmp/hashi-browser-bridge.sock`
- native host log: `logs/browser_native_host.log`
- direct bridge calls through `tools.browser_extension_bridge`

Example:

```bash
python3 - <<'PY'
from tools.browser_extension_bridge import healthcheck, send_bridge_command
import json

print(json.dumps(healthcheck(socket_path='/tmp/hashi-browser-bridge.sock'), indent=2))
print(json.dumps(send_bridge_command('active_tab', {}, socket_path='/tmp/hashi-browser-bridge.sock'), ensure_ascii=False, indent=2))
PY
```

## Current verified action surface

- `active_tab`
- `get_text`
- `get_html`
- `screenshot`
- `click`
- `fill`
- `evaluate`

## Real Windows desktop workflow

1. Use the Windows tool tier to focus the real Chrome window.
2. Navigate to a real site first, not `chrome://`:
   - `https://www.wikipedia.org/`
   - `https://scholar.google.com/`
   - `https://arxiv.org/`
3. Validate the bridge from WSL with `healthcheck(...)`.
4. Validate actions through direct bridge calls or `tools/browser.py` executors.

## Unpacked extension update workflow

When the unpacked extension files have changed on disk but the running bridge still reports:

```text
unsupported action: <action>
```

use this Windows live update sequence:

1. Focus the real Chrome window.
2. Open `chrome://extensions`.
3. Press `Tab` 11 times.
4. Press `Enter`.

On this host, that sequence reliably caused Chrome to adopt the updated service worker code.

## Verified live results

The Windows live bridge has been directly verified to:

- fill the Wikipedia search input with `OpenAI`
- evaluate `document.title` and return `Wikipedia`
- evaluate `JSON.stringify({title: document.title})` and return serialized JSON
- click the Wikipedia search button and navigate to search results

## Caveats

- `chrome://` pages are often non-scriptable by design.
- `screenshot` can fail on internal Chrome pages before page-level invocation takes effect.
- Windows desktop screenshots can be misleading:
  - another top-level window may remain visible
  - Chrome can still be the target receiving keyboard input
- When UI and bridge disagree, trust:
  - bridge response
  - socket status
  - native host log
  - Chrome profile extension state
