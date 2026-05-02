# HASHI

This is `HASHI（develop code name bridge-u-f)`, a local multi-agent bridge.

## Agent Seeds and the Soul Repository
- **Seed Location**: `/agent_seeds/`
- **Contents**: pre-defined "Soul" templates (e.g., Zelda, Samantha, Jarvis, Pikachu).
- **Purpose**: These serve as permanent blueprints for the system's specialized personas. They are preserved during a NReset.
- **Deployment Procedure**:
  1. Read the character's seed `.md` file from `agent_seeds/`.
  2. Create a dedicated directory in `workspaces/<agent_id>/`.
  3. Write the seed content into `workspaces/<agent_id>/AGENT.md`.
  4. Update `agents.json` by adding a new entry to the `agents` list. Refer to `agents.json.samples` for the standard Flex Agent schema. The agent's "system_md" must point to `workspaces/<agent_id>/AGENT.md`
  5. Configure credentials in `secrets.json`. Use `WORKBENCH_ONLY_NO_TOKEN` if a Telegram token is not yet available.
  6. Ask the user to restart.

## Agent Types
- Flex agent: one bot, one workspace, switchable backend via `/backend`.
- Fixed agent: one bot, one backend, one workspace.

## Important Commands
- `/help`: command list for this agent.
- `/new`: fresh CLI session reset. Use this for CLI-backed agents (`claude-cli`, `gemini-cli`, `codex-cli`).
- `/fresh`: clean API context for non-CLI backends (`openrouter-api`, `deepseek-api`, `ollama-api`). Clears recent turns and stops saved memories from being auto-injected without deleting them.
- `/handoff`: fresh continuity restore from recent chat history.
- `/fyi [prompt]`: explicit bridge environment awareness refresh.
- `/active [on|off] [minutes]`: toggle proactive follow-up heartbeat; default is 10 minutes.
- `/voice [status|on|off|provider|providers|voices|use <alias>]`: control native bridge-owned voice replies.
- `/retry`: resend last response or rerun last prompt.
- `/debug <prompt>`: strict debug mode with verification-first behavior.
- `/usecomputer [on|off|status|examples|task]`: load managed GUI-aware operating guidance. This is a unified shortcut for desktop/browser/Windows computer use, but it does not force GUI when a better non-GUI path exists.
- `/skill`: browse built-in and custom skills.
- `/model`: inspect or switch model where supported.
- `/verbose [on|off]`: toggle richer long-task status display.
- `/think [on|off]`: toggle thinking trace display — periodic italic messages showing model reasoning (~60s intervals). Independent from `/verbose`.
- `/stop`: cancel current processing.
- `/start`: start another stopped agent.
- `/reboot`: hot restart agents with live Python code reload. Modes:
  - `/reboot` — restart all running agents (same selection), picks up code + config changes.
  - `/reboot min` — restart only this bot.
  - `/reboot max` — restart all active agents.
  - `/reboot [number]` — restart a specific agent by number.
  - `/reboot help` — list modes and show all agents with numbers.
- `/terminate`: shut down this agent.

## Flex-Only Commands
- `/backend`: open backend picker, then model picker, then commit the switch.
- backend `+`: same flow, but rebuild handoff context after model confirmation.
- `/model`: inspect or switch the model for the current active backend only.
- `/effort`: available when active backend supports effort levels, currently Claude or Codex.

## Flex Backend Behavior
- Flex backend switching is atomic: backend choice is not committed until a valid model is selected.
- `/backend` edits the same Telegram flow into a backend-specific model picker.
- `/backend +` preserves the handoff intent through that picker and applies it only after the switch succeeds.
- Backend rollback exists: if the new backend fails to initialize, bridge restores the previous backend.
- Flex backend state persists in `workspaces/<agent>/state.json`.
- Persisted state includes:
  - `active_backend`
  - per-backend selected `model`
  - per-backend selected `effort` where supported
- OpenRouter key lookup order for flex agents is:
  - `<agent_name>_openrouter_key`
  - `openrouter-api_key`
  - `openrouter_key`
- Default OpenRouter model is `anthropic/claude-sonnet-4.6`.

## Core Memory Model
- Backend CLI/API sessions are treated as stateless by bridge.
- Bridge owns continuity and context injection.
- `/new` starts a fresh CLI session and re-primes the agent with this FYI catalog.
- `/fresh` starts a clean API context. It clears recent turns, preserves saved memories, and disables saved-memory auto-injection until `/memory saved on` or `/memory on` restores it.
- `/handoff` restores recent continuity from bridge transcript, not CLI resume state.
- `/fyi` explicitly refreshes awareness of this bridge environment and can carry a follow-up prompt.

## Skills System
- Skills live under `skills/`.
- Types: `action`, `prompt`, `toggle`.
- Built-ins currently include:
  - `cron`
  - `heartbeat`
  - `debug`
  - `recall`
- Toggle skills persist in workspace state until turned off.
- `/skill` is the main browser for the skill catalog.
- `recall` is a bridge policy toggle: if ON, recent continuity is auto-restored once after an unexpected restart, but not after `/new` or `/fresh`.

## Workspaces And Files
- Main repo guide: `README.md`
- Agent config: `agents.json`
- Scheduler tasks: `tasks.json`
- Fixed transcript: `conversation_log.jsonl`
- Flex transcript: `transcript.jsonl`
- Flex continuity files:
  - `recent_context.jsonl`
  - `handoff.md`
- Logs: `logs/<agent>/<session>/`

## Scheduling
- Scheduler reads `tasks.json`.
- Cron and heartbeat jobs can enqueue prompts or invoke skills.
- Built-in skill views can inspect and toggle cron/heartbeat jobs.
- `/active on` creates or enables a managed heartbeat job for this agent.

## Browser Tool

Agents can control a real web browser (headless or headed) using Playwright.

**Two modes:**
- *Standalone* — launches a clean headless Chromium (no login state)
- *CDP mode* — attaches to the user's running Chrome, reusing all cookies and login sessions

**For CLI-backend agents (Claude CLI, Gemini CLI, Codex CLI)** — use `bash` to call the wrapper:
```bash
python tools/browser_cli.py screenshot --url <url> [--out /tmp/shot.png]
python tools/browser_cli.py get_text   --url <url> [--cdp-url http://localhost:9222]
python tools/browser_cli.py get_html   --url <url>
python tools/browser_cli.py click      --url <url> --selector <css>
python tools/browser_cli.py fill       --url <url> --selector <css> --text <text> [--submit]
python tools/browser_cli.py evaluate   --url <url> --script "() => document.title"
```

**For OpenRouter API agents** — add to `agents.json` `tools.allowed`:
```json
"allowed": ["browser_screenshot", "browser_get_text", "browser_get_html",
            "browser_click", "browser_fill", "browser_evaluate"]
```

**CDP mode (reuse user's logged-in browser):**
1. Start Chrome once: `google-chrome --remote-debugging-port=9222 --user-data-dir=~/.chrome-hashi`
2. Pass `--cdp-url http://localhost:9222` to any browser command

**Prerequisites:** `playwright install chromium` (one-time setup).

## Usecomputer Command

`/usecomputer` is the consolidated operator-facing shortcut for "use the computer like a human if needed".

- It activates managed guidance through `/sys 10`.
- It is a prompt-level operating mode, not a separate tool tier.
- It tells the agent to prefer non-GUI methods first, then use `desktop_*` or `windows_*` when GUI interaction is actually the best path.
- Alias: `/usercomputer`

Supported forms:
- `/usecomputer on`
- `/usecomputer off`
- `/usecomputer status`
- `/usecomputer examples`
- `/usecomputer <task>`

When GUI work is needed, the expected behavior is:
- inspect the environment first
- choose the correct family: `desktop_*` for Linux/X11 virtual desktop, `windows_*` for the real Windows desktop
- verify window focus and screenshots before acting
- work in small reversible steps
- re-check after important actions instead of assuming state

## Desktop Tool

Agents can control a Linux virtual desktop (Xvfb or XRDP session) using the `desktop_*` tool tier.
This is fully independent of the Windows host — it works even when the Windows screen is locked.

This tier is one of the backends that `/usecomputer` may choose when Linux/X11 desktop interaction is the right method.

**Available tools:** `desktop_screenshot`, `desktop_click`, `desktop_type`, `desktop_key`, `desktop_mouse_move`, `desktop_scroll`, `desktop_info`

**For CLI-backend agents (Claude CLI, Gemini CLI, Codex CLI)** — use `bash` directly:
```bash
DISPLAY=:10 ~/projects/hashi2/tools/bin/usecomputer screenshot /tmp/shot.png --json
DISPLAY=:10 xdotool type "hello world"
DISPLAY=:10 ~/projects/hashi2/tools/bin/usecomputer press "ctrl+s"
```

**For OpenRouter API agents** — add the `desktop` tier to `agents.json`:
```json
"tools": {
  "tiers": ["core", "desktop"],
  "max_loops": 15
}
```

**DISPLAY resolution** (automatic, override with `HASHI_DESKTOP_DISPLAY` env var):
- Prefers `:10` (XRDP/Xvfb virtual session — works when Windows is locked)
- Falls back to `:0` (WSLg — requires Windows unlocked)

**Start a persistent virtual desktop:**
```bash
Xvfb :10 -screen 0 1920x1080x24 -ac &
DISPLAY=:10 WAYLAND_DISPLAY="" dbus-launch xfwm4 &
```

**Keyboard note:** `desktop_type` uses `xdotool` for full Unicode/space/symbol support.
Requires `xdotool` installed: `sudo apt-get install -y xdotool`

**Binary:** vendored at `tools/bin/usecomputer` (MIT license, native Zig binary, no runtime deps).

## Windows Use Tool

Agents can control the real Windows desktop through the `windows_*` tool tier.
This is designed for HASHI agents running either directly on Windows or inside WSL.

This tier is one of the backends that `/usecomputer` may choose when real Windows desktop interaction is the right method.

**Available tools:** `windows_screenshot`, `windows_click`, `windows_type`, `windows_key`, `windows_mouse_move`, `windows_scroll`, `windows_info`, `windows_window_list`, `windows_window_focus`, `windows_window_close`

**Current backends:** `usecomputer` plus `windows-mcp` on the Windows host, launched through `powershell.exe`.

**Important behavior:**
- Intended for the real interactive Windows desktop, not the Linux virtual desktop.
- Best reliability when Windows is unlocked.
- From WSL, tool calls cross the WSL ↔ Windows boundary automatically.
- `provider=auto` picks the smoother backend per action.
- Treat multi-display Windows setups as normal, not exceptional.
- Before any screenshot-led Windows task, call `windows_info` first and inspect `displays`.
- Decide which display should contain the target window, then use `windows_screenshot(display=N)` for that monitor before clicking or typing.
- Pair screenshots with `windows_window_list` / `windows_window_focus`; a screenshot alone is not proof that focus landed on the expected window or monitor.
- `windows_type` can focus a target window first.
- `windows_window_close` supports optional unsaved-prompt dismissal and explicit force close.

**For OpenRouter API agents** — add the `windows_use` tier to `agents.json`:
```json
"tools": {
  "tiers": ["core", "windows_use"],
  "max_loops": 15
}
```

**Current environment requirement on Windows host:**
```powershell
npm install -g usecomputer
```

**Effective Windows Chrome extension workflow (known good):**

1. Use `windows_info` first and inspect `displays`.
2. Choose the display that should contain the real Chrome window and capture `windows_screenshot(display=N)` for that screen.
3. Use `windows_window_list` to find a Chrome window.
4. Use `windows_window_focus` to bring that window forward.
5. Re-capture `windows_screenshot(display=N)` before typing if focus or monitor placement matters.
6. Navigate with:
   - `windows_key` → `ctrl+l`
   - `windows_type` → target URL
   - `windows_key` → `ENTER`
7. Prefer a real site for bridge verification:
   - `https://scholar.google.com`
   - `https://www.wikipedia.org/`
   - `https://arxiv.org/`
8. Verify the bridge from WSL/Linux side with bridge-owned evidence:
```bash
python3 - <<'PY'
from tools.browser_extension_bridge import healthcheck, send_bridge_command
import json
print(json.dumps(healthcheck(socket_path='/tmp/hashi-browser-bridge.sock'), indent=2))
print(json.dumps(send_bridge_command('active_tab', {}, socket_path='/tmp/hashi-browser-bridge.sock'), ensure_ascii=False)[:2000])
PY
```

**Known good Windows live socket:**
- `/tmp/hashi-browser-bridge.sock`

**Known good Windows live extension action surface:**
- `active_tab`
- `get_text`
- `get_html`
- `screenshot`
- `click`
- `fill`
- `evaluate`

**Important control detail:**
- `active_tab(args.url=...)` is a live control action.
- It updates the real active Windows Chrome tab to the target URL and waits for completion.
- This means the extension can be used on authenticated sites too, if the user's Chrome session is already logged in.

**Known live extension id on this host:**
- `jdeaedmoejdapldleofeggedgenogpka`

**Important Windows live cautions:**
- `chrome://` pages are usually non-scriptable:
  - `get_text` may fail there by design
- `screenshot` can fail on `chrome://newtab/` or other internal pages before the extension has an effective page invocation
- The visible Chrome UI may be misleading:
  - a narrow side-window or suggestions overlay can remain on screen
  - the bridge may still be fully healthy behind that UI
- If UI and bridge state disagree, trust bridge-owned evidence first:
  - `/tmp/hashi-browser-bridge.sock`
  - `logs/browser_native_host.log`
  - Chrome profile `Secure Preferences`

**Known good Windows live outcomes already verified:**
- `active_tab` on real sites
- `get_text` on Google Scholar / Wikipedia / arXiv
- `screenshot` on Wikipedia / arXiv
- Gmail inbox access through the already logged-in Chrome session
- `fill` on the Wikipedia search input
- `evaluate('document.title')` returning `Wikipedia`
- `evaluate('JSON.stringify({title: document.title})')` returning serialized JSON
- `click('button[type="submit"]')` causing real navigation to Wikipedia search results

**Important unpacked-extension update detail:**
- after changing service worker code on disk, a plain Chrome restart was not always enough for the running unpacked extension
- on this host, the reliable update sequence was:
  - focus the real Chrome window
  - open `chrome://extensions`
  - press `Tab` 11 times
  - press `Enter`
- this was the step that changed live Windows behavior from:
  - `unsupported action: fill`
  - `unsupported action: evaluate`
  to:
  - working `fill`, `click`, and `evaluate`
- for `evaluate`, the known-good implementation uses `chrome.scripting.executeScript(..., world: "MAIN")`

**Practical rule moving forward:**
- Do not assume the Windows desktop view alone tells the truth.
- Always pair `windows_*` actions with:
  - bridge socket checks
  - native host log checks
  - direct `send_bridge_command(...)` verification

## Telegram File Sending

Agents can send photos, documents, videos, and audio files to the user via Telegram.

**For CLI-backend agents (Claude CLI, Gemini CLI, Codex CLI)** — use `bash` to call the wrapper:
```bash
python tools/telegram_send_file_cli.py --path /tmp/chart.png
python tools/telegram_send_file_cli.py --path /tmp/chart.png --caption "Daily report" --agent <your_name>
python tools/telegram_send_file_cli.py --path /tmp/doc.pdf --type document
```

Parameters:
- `--path` (required): absolute path to the file
- `--caption` (optional): message caption
- `--type` (optional): `photo`, `document`, `video`, `audio` (default: auto-detect from extension)
- `--agent` (optional): your agent name for token resolution

Auto-detection: `.jpg/.png/.webp` → photo, `.mp4/.mov` → video, `.mp3/.ogg/.wav` → audio, everything else → document.

**For OpenRouter/DeepSeek API agents** — `telegram_send_file` is auto-injected via global `default_tools` in `agents.json`. No per-agent config needed. Use it as a native tool call:
```json
{"tool": "telegram_send_file", "path": "/tmp/chart.png", "caption": "Daily report"}
```

## Media
- Agents can receive text plus Telegram media.
- Voice/audio is transcribed locally before being sent to the backend.
- Photos, documents, audio, video, and stickers are supported.
- Outbound spoken replies can be bridge-generated locally and delivered through supported transports when voice mode is enabled.
- Voice is bridge-owned capability: models still return text, and bridge handles synthesis, OGG/Opus conversion, and transport delivery.
- Voice providers are pluggable; built-ins include `edge`, `piper` ect.

## WhatsApp Linking Procedure

**Do NOT run `link_whatsapp.py` directly.** It starts an interactive pairing session that will hang indefinitely when run as a subprocess — the agent can't display the QR and will never exit.

**Correct method:**
1. Run `scripts/run_whatsapp_link.sh` in the background — this starts `link_whatsapp.py` with `--qr-image-file /tmp/wa_link_qr.png --completion-file /tmp/wa_link_result.json`
2. Poll for `/tmp/wa_link_qr.png` to appear (within ~5 seconds)
3. Send that PNG file to the user via Telegram (`send_photo`)
4. Poll `/tmp/wa_link_result.json` — when `{"status": "linked"}` appears, notify the user that WhatsApp is connected
5. If `{"status": "timeout"}` appears, tell the user to try again

Session is saved in `wa_session/` — subsequent starts do not need a QR scan.

## 记忆查询与上报（通过小蕾 / lily）

小蕾（lily，HASHI1）是系统唯一的记忆守护者，管理所有 agent 的长期巩固记忆。

### 查询记忆

如果你需要查询系统知识、项目信息、或自己的历史记忆，向小蕾发送请求。

**方法：**
- **同实例（HASHI1）：** 直接发消息给 lily
- **跨实例（HASHI2/HASHI9等）：** 优先使用 Hchat / Workbench live chat 联系 HASHI1 的 lily，或使用 `/ask lily 你的问题`

**查询权限：**
- system 域（系统知识）：所有 agent 可查询
- project 域（项目信息）：相关 agent 可查询
- personal / identity 域：只能查询自己的，不能查看其他 agent 的

### 上报信息

如果你在工作中发现重要的系统变更或项目进展，主动报告给小蕾存档。

**可上报：** 系统变更（配置、端口、新 agent 等）、项目进展（阶段完成、问题、决策等）
**不可上报：** 其他 agent 的 personal / identity 信息

小蕾收到后会核实并存储。如与现有记录冲突，她会暂缓存储并请求爸爸批准。

**禁止：** 不要直接读取其他 agent 的 workspace 或数据库文件。

## Practical Expectations
- Prefer bridge-owned evidence: code, logs, config, transcripts.
- Use `README.md` when you need deeper detail, or the user has system related questions.
- Do not assume CLI internal session memory is available or reliable.

## IT Support - /ticket

Arale serves as the system's IT Support agent. When you encounter a technical issue (backend crash, timeout, config error, etc.), use `/ticket` to submit a support request.

**Usage:** `/ticket <problem description>`

**What happens automatically (program-driven, no LLM required):**
1. Orchestrator collects diagnostic info: last error log, backend status, recent context, git status, system resources
2. A ticket JSON is written to `tickets/open/`
3. Arale is notified via bridge message (file fallback if bridge is down)

**Auto-trigger (no user action needed):**
- Backend process crash → ticket created automatically
- 3 consecutive request timeouts → ticket created automatically

**Arale's response protocol:**
- Confidence ≥90%: fixes directly (restart process, modify config — no business code changes, no PC restart)
- Confidence <90%: provides recommendation for admin approval

**You do NOT need to:**
- Collect logs yourself — the system does it
- Tag priority — auto-assessed
- Follow up — Arale will investigate and respond

**Ticket statuses:** `open` → `in_progress` → `resolved`

## Mailbox Status

Cross-Instance Mailbox is retired.

- Do not use mailbox for cross-instance delivery.
- Do not document mailbox as a fallback path.
- Hchat / Workbench live chat is the official inter-instance protocol.

## Hchat — Real-Time Direct Agent Messaging

**Hchat** is the official name for real-time direct messaging between agents across HASHI instances via HTTP API.

The formal protocol is:

- Identity and routing metadata are separate.
- Workbench `/api/chat` is the final delivery surface.
- `instances.json + agents.json + live health` are authoritative.
- `contacts.json` is only a short-lived cache.
- `Remote /hchat` is a restricted-network fallback for LAN / internet relay.
- Mailbox is retired and banned from the formal protocol.
- `name` means local delivery only. Do not guess cross-instance targets.
- `name@INSTANCE` means cross-instance delivery and must go through the `HASHI1` exchange.
- If the sender included `@INSTANCE`, replies must preserve that instance identity and must not be redirected to a same-name local agent.

### Prerequisites — WSL Mirrored Networking

For `127.0.0.1` to be shared between WSL and Windows, `.wslconfig` must have `networkingMode=mirrored`.

**File:** `C:\Users\<user>\.wslconfig`
```ini
[wsl2]
networkingMode=mirrored
```

After editing, restart WSL: shut down via `wsl --shutdown` in PowerShell, then relaunch.

**Status:** ✅ Confirmed working as of 2026-03-24. WSL and Windows now share `127.0.0.1`.

### HASHI9 API Endpoints (from WSL, after mirrored networking)

| Port | Purpose | Example |
|------|---------|---------|
| `18819` | Workbench API — chat with agents | `POST http://127.0.0.1:18819/api/chat` |
| `18801` | API Gateway — OpenAI-compatible interface | `POST http://127.0.0.1:18801/v1/chat/completions` |

### Sending a Real-Time Message to HASHI9

```bash
curl -s -X POST http://127.0.0.1:18819/api/chat \
  -H "Content-Type: application/json" \
  -d '{"agent": "hashiko", "text": "你好！"}'
```

### Communication Protocol Summary

| Method | Priority | Use Case |
|--------|----------|---------|
| **Workbench `/api/chat`** | Primary | Same-instance delivery |
| **`HASHI1` exchange** | Primary for cross-instance | Any `agent@INSTANCE` delivery between `HASHI1/HASHI2/HASHI9/MSI` |
| **`contacts.json` cache** | Secondary | Recently learned routes, refreshed against registry before use |
| **Remote `/hchat`** | Transport | Carry exchange traffic or restricted-network relay |

Do not fall back to mailbox.
