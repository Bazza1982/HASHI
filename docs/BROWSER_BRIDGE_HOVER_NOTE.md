# HASHI Browser Bridge — extension `hover`

**Date:** 2026-07-15 (design) · 2026-07-19 (landed)  
**Status:** Implemented in production extension source **v0.1.2**  
**Live reload:** reload the extension in `chrome://extensions` after pull; LinkedIn Celebrate e2e still optional

## Why this matters (LinkedIn)

| Step | LinkedIn UI | Bridge |
|------|-------------|--------|
| 1 | Hover (or long-hold) main reaction control | ✅ `hover` (CDP `mouseMoved`) |
| 2 | Picker injects Celebrate / Love / … into DOM | ✅ after `wait_ms` (~400–800) |
| 3 | Short click target reaction | ✅ existing `click` |

Plain `click` on the main reaction = **Like only**.  
`click` on `Open reactions menu` often returns OK but does **not** expand the picker.  
Synthetic `evaluate` mouse events on LinkedIn are weak (MAIN-world CSP / untrusted events).

**Product note:** LinkedIn cron may stay Like-only until operators enable multi-reaction SOPs that call `hover` → `click`.

## Extension actions (v0.1.2)

Source: `tools/chrome_extension/hashi_browser_bridge/service_worker.js`

| Action | Status |
|--------|--------|
| `active_tab` / `session*` | ✅ |
| `get_text` / `get_html` | ✅ isolated `executeScript` |
| `click` | ✅ `querySelector` + `element.click()` |
| `fill` | ✅ |
| `type_text` | ✅ CDP `Input.insertText` |
| `evaluate` | ✅ MAIN world `eval` (LinkedIn often returns empty) |
| `screenshot` | ✅ |
| **`hover`** | ✅ CDP `Input.dispatchMouseEvent` (`mouseMoved`) |
| `scroll` / `key` / `select` / `wait_for` / `get_attribute` / `long_press` | ❌ not in extension path |

Docs truth table: [OPTION_D_BROWSER_BRIDGE.md](OPTION_D_BROWSER_BRIDGE.md).

Python client (`tools/browser_extension_bridge.py`) is action-agnostic: it sends `{action, args}` over the socket.

## API: `hover`

```text
hover
  args:
    selector: string      # CSS (required)
    timeout_ms?: number   # wait for element; default 10000
    wait_ms?: number      # pause after mouse move for flyouts; default 500
    x_ratio?: number      # 0–1 within element box; default 0.5
    y_ratio?: number      # default 0.5
  returns:
    output: "OK: hovered '<selector>'"
    meta: { action, selector, details: { x, y, tagName, rect, ... } }
```

### LinkedIn Celebrate sequence (agent use)

```text
1. hover  button[componentkey="<main-reaction-ck>"]   wait_ms=600
2. get_html / get_text → find Celebrate / Love control
3. click  <celebrate selector>
4. verify  aria-label="Reaction button state: Celebrate" (or Love)
```

Optional later: `long_press` = pointerdown + hold_ms + pointerup.  
**Desktop LinkedIn path is hover + short click** — `long_press` deferred.

## Implementation notes

1. `resolveTab` is called with `wait_ms: 0` so the post-hover pause is not consumed during tab resolve.
2. Focus tab + window before move (mirrors screenshot focus pattern).
3. Resolve element + viewport CSS coords via **CDP `Runtime.evaluate`** (not mixed `chrome.scripting` return values — React-heavy pages sometimes omit scripting results).
4. `scrollIntoView` center, then `Input.dispatchMouseEvent` type `mouseMoved` at `(x, y)`.
5. `sleep(wait_ms)` so React flyouts can mount.
6. Hover does **not** click.

### Surfaces wired

| Layer | Change |
|-------|--------|
| Extension `actionHover` | `service_worker.js` · version **0.1.2** |
| Native host mutating set | `hover` added to `MUTATING_ACTIONS` |
| Tool schema | `timeout_ms`, `wait_ms`, `x_ratio`, `y_ratio` on hover tool |
| Playwright fallback | honors `wait_ms` after `page.hover` |
| CLI | passes timeout/wait/x_ratio/y_ratio into hover |
| Tests | `tests/test_browser_extension_actions.py`, `tests/test_browser_extension_hover_source.py` |

### Coordinate caveats

- CDP mouse events use **CSS pixels** relative to the viewport (`getBoundingClientRect`).
- Window must be focused to avoid wrong target.
- HiDPI mismatches: check `devicePixelRatio` / `Page.getLayoutMetrics` if coords miss.

## Out of scope for v0.1.2 hover

- Multi-monitor UIA coordinate mapping
- Auto multi-reaction in LinkedIn cron
- Full Playwright parity (`drag`, `upload`, extension `scroll`/`key`/…)

## Acceptance / smoke

1. Reload extension **0.1.2** (`chrome://extensions`).
2. LinkedIn feed, unliked post: `hover` main reaction → DOM contains Celebrate.
3. `click` Celebrate → state Celebrate (not only Like).
4. Regression: plain Like `click` still works without hover.
5. Unit/source tests: `pytest tests/test_browser_extension_actions.py tests/test_browser_extension_hover_source.py`.

## Decision log

- **2026-07-15 Barry:** LinkedIn Celebrate = hover → locate → short click; investigate bridge hover; keep cron Like-only until ready.
- **2026-07-19:** Extension `hover` landed (CDP mouseMoved + timing/position args); docs and schema aligned; live LinkedIn Celebrate e2e optional operator check after reload.
