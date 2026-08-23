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
  4. Update `agents.json` by adding a new entry to the `agents` list. Refer to `agents.json.samples` for the standard Flex Agent schema. The Agent's `system_md` must point to its explicit Persona file; filenames such as `agent.md`, `AGENT.md`, and custom configured paths are all valid.
  5. Configure credentials in `secrets.json`. Use `WORKBENCH_ONLY_NO_TOKEN` if a Telegram token is not yet available.
  6. Ask the user to restart.

## Agent Types and Runtime Modes
- Flex agent: one bot, one workspace, switchable backend via `/backend`.
- Fixed agent: one bot, one backend, one workspace.
- Runtime execution modes are `fixed`, `flex`, `wrapper`, `audit`, and
  `dual-brain`. Memory+ is an independent continuity setting, not a sixth mode.

## Important Commands
- `/help`: command list for this agent.
- `/new`: fresh CLI session reset. Use this for CLI-backed agents (`claude-cli`, `gemini-cli`, `codex-cli`).
- `/fresh`: clean API context for non-CLI backends (`openrouter-api`, `deepseek-api`, `ollama-api`). Clears recent turns and stops saved memories from being auto-injected without deleting them.
- `/handoff`: fresh continuity restore from recent chat history.
- `/fyi [prompt]`: explicit bridge environment awareness refresh.
- `/bg <task>`: queue a background-capable task. Treat `/bg <task>` as `/bg run <task>`; preserve the user's task text exactly and use HASHI BackgroundJobManager for long OS/process work instead of blocking the chat. If model-facing `background_job_*` tools are unavailable, use the live local Workbench `/api/background-jobs` endpoints instead of starting a temporary standalone manager. Start managed jobs with success/failure notification and completion/failure agent-event routing enabled when possible.
- `/bg status [job_id]`, `/bg tail <job_id>`, `/bg cancel <job_id>`, `/bg list`: inspect or manage recorded background jobs.
- `background-job-event`: internal one-shot event delivered when a managed background job reaches a terminal state. Use the included `job_id`, `event`, `returncode`, paths, and `last_output` to summarize, continue the workflow, ask for confirmation, or report failure. Only run extra inspection when the event evidence is missing or inconsistent, and do not restart the same job unless the user explicitly asked for that behavior.
- `/active [on|off] [minutes]`: toggle proactive follow-up heartbeat; default is 10 minutes.
- `/voice [status|on|off|provider|providers|voices|use <alias>]`: control native bridge-owned voice replies.
- `/say`: read the last assistant reply as a voice message. This forces one TTS attempt even when `/voice off`, as long as a usable voice provider and voice choice are configured.
- `/resend`: replay the previous model or Bridge output exactly, without model work.
- `/retry`: stop stale execution, reset to a clean CLI/API context, restore recent handoff continuity, and rerun the last prompt. Full reference for both recovery commands: [RETRY_RESEND_COMMANDS.md](RETRY_RESEND_COMMANDS.md).
- `/debug <prompt>`: strict debug mode with verification-first behavior.
- `/usecomputer [on|off|status|examples|task]`: load managed GUI-aware operating guidance. This is a unified shortcut for desktop/browser/Windows computer use, but it does not force GUI when a better non-GUI path exists.
- `/browser [status|examples|1-4 task]`: run an internet task with a selected route: HASHI headless browser, CLI-native browsing, Brave search, or the logged-in HASHI browser extension.
- `/exp <task>`: consult the context-specific EXP guidebooks under `exp/` before running a task. Large binary training/evidence assets are optional packs; check `python scripts/exp_assets.py status` and restore only when the selected guidebook needs them.
- `/skill`: browse, apply, validate, enable/disable, install/link, and safely uninstall standard instruction Skills.
- `/mode [fixed|flex|wrapper|audit|dual-brain]`: inspect or switch execution
  mode. `/mode memory+` is a compatibility alias that enables continuity
  without changing the current mode.
- `/memory [status|on|pause|saved on|saved off|plus on|plus off]`: control
  normal memory injection and Memory+ continuity independently.
- `/notepad [today|carryover|history|find <query>|edit <text>|replace <text>|compact|clear]`:
  inspect or maintain the bounded Memory+ work card and archive index.
- `/model`: on HER v2, define complete Quick/Pro provider/model targets and
  independently choose Follow Quick, Follow Pro, or Custom plus provider
  reasoning for each effective task route; on other
  backends, retain the existing single-model behaviour.
- `/habit [view|on|off|default|delete|reset]`: inspect or control the default-off,
  adapter-owned HER Habit/Meditation path. Non-HER backends do not read or
  modify its records.
- `/superloop`: recording-first long-running workflow orchestration.
- `/verbose [on|off]`: toggle bounded technical planning, tool, test, validation,
  retry, and runtime telemetry. HER Persona speech and reasoning are excluded.
- `/think [on|off]`: toggle the current backend's reasoning presentation. For
  HER this means genuine provider-returned reasoning only; generic progress and
  tools remain under `/verbose`.
- `/commentary [on|off]`: HER-only presentation of explicitly model-authored
  Persona acknowledgements and interim reports. Each logical event is durable
  and delivered at most once, independently from `/think` and `/verbose`; other
  backends retain their own display rules.
- `/typing [on|off|status]`: control both the temporary typing bubble and Telegram's native typing indicator, independently from `/verbose` and `/think`.
- `/stop`: cancel current processing. HASHI durably preserves the interrupted user task;
  a later plain `continue`, `resume`, or `继续` request is explicitly rebound to that task,
  including after a runtime restart. An unrelated new request remains unrelated. The
  intentional process kill is not reported as a Backend error.
- `/steer <direction>`: when busy, stop the current turn immediately (all backends), keep interim thinking/progress/artefacts, then continue with a mid-task wrapper. When idle, send the direction as a plain new request (no steer wrapper). Example: `/steer also include unit tests`. The intentional kill (e.g. exit `-9`) is suppressed — you should see the steer ack, not `❌ Backend error`. Full reference: [STEER_COMMAND.md](STEER_COMMAND.md).
- `/focus`: one-off scope correction that does not cancel or finish the task. When busy, replace the active backend turn with an immediate continuation that preserves progress/artefacts, narrows execution to the original user-requested scope, and keeps working until the requested outcome is complete or genuinely blocked. When idle, apply the same continuation reminder to the most recent task; if no task is available, do nothing. Full reference: [FOCUS_RECALL_COMMANDS.md](FOCUS_RECALL_COMMANDS.md).
- `/delay <minutes> <message>`: persist a message in this agent's FUTURE queue, then append it to the normal READY FIFO no earlier than its due time. Use `/delay list` or `/delay cancel <delay-id>` to inspect or cancel it. Delays survive restart, do not make the agent busy, use the backend/configuration active at dispatch time, and never alter cron, heartbeat, nudge, or other `/jobs` records. A delayed payload beginning with `/` is delivered to the agent as text rather than executed as another command. Full reference: [DELAY_COMMAND.md](DELAY_COMMAND.md).
- `/queue [list|show <id>|cancel <id>|clear|history]`: inspect or manage both READY requests and FUTURE delayed messages. The active request is never interrupted.
- `/recall [count]`: remove requests still waiting in this agent's READY or FUTURE queue without interrupting the current task. With no count, remove both layers completely. The optional count may be any positive whole number and selects the newest requests across both layers by creation time. If `n` exceeds the combined queue length, all waiting requests are removed without error. Retained READY requests keep their original FIFO order. It does not restart the backend or affect cron, heartbeat, nudge, or other `/jobs` work. This command is separate from the hidden legacy recall-state compatibility setting. Full reference: [FOCUS_RECALL_COMMANDS.md](FOCUS_RECALL_COMMANDS.md).
- `/privacy [0-5]`: show the privacy menu and active-backend compatibility details, or request a level directly. Level 0 disables the privacy framework; Level 1 is the default provider-trust mode with no local redaction. Levels 2–5 remain visible as reserved framework states and cannot be activated until their promised controls are installed and verified. Lowering the active level requires explicit confirmation.
- `/start`: start another stopped agent.
- `/reboot`: hot restart agents with live Python code reload. Modes:
  - `/reboot` — restart all running agents (same selection), picks up code + config changes.
  - `/reboot min` — reload current code/config and restart only this bot; it is
    never promoted to all bots or blocked merely because a class interface
    changed.
  - `/reboot max` — restart all active agents.
  - `/reboot [number]` — reload current code/config and restart exactly that agent.
  - `/reboot help` — list modes and show all agents with numbers.
- `/rebuild`: one-version compatibility notice for the retired native HER build workflow. It performs no build, reload, restart, or status lookup. Use `/reboot` to adopt HASHI Python updates.
- `/terminate`: shut down this agent.

## Backend and Model Configuration
- `/backend`: in Flex, open the backend picker. HER v2 commits the backend
  directly without exposing `role-configured`; other backends retain their
  existing model-selection flow. In another execution mode, first ask for
  confirmation to move to Flex; saved specialized-mode configuration and
  Memory+ are preserved.
- backend `+`: same flow, but rebuild handoff context after the backend switch.
- `/provider`: while HER v2 is active, select a Single call provider or open a
  Hybrid routing draft. Instance configuration is sufficient; a provider need
  not be repeated in the Agent's backend list.
- `/model`: while HER v2 is active, define Quick/Pro provider/model targets and
  configure each effective task route's target and provider reasoning
  separately. Hybrid edits take effect together through Apply. Other
  active backends retain their existing behaviour.
- Backend and model changes continue to an optional effort picker when the
  selected model supports effort. Keeping the current value leaves it unchanged;
  models without selectable effort finish with `n/a`.
- `/effort [level]`: available when the active backend supports effort levels. Grok CLI offers `low`, `medium`, and `high` with a HASHI default of `medium`. Codex choices follow the active model. HER v2 effort controls orchestration depth, Replanning, Review, and sub-agent availability; it never changes provider reasoning configured through `/model`.
- Grok CLI `0.2.93` offers `grok-4.5` as the default model and
  `grok-composer-2.5-fast` as an alternate. An agent explicitly configured for
  Composer keeps that choice until `/model grok-4.5` is selected. `/effort`
  changes Grok CLI reasoning effort and persists that backend choice.

## HASHI API Caller-Owned Tools

- The OpenAI-compatible `POST /v1/chat/completions` route supports
  caller-owned function tools on Codex CLI models and compatible xAI Chat
  Completions models. Other CLI engines and xAI Responses API models fail
  explicitly instead of losing tool schemas.
- HASHI returns standard assistant `tool_calls`; it never executes these API
  client functions. The calling Agent/application owns name allow-listing,
  argument validation, authorization, execution, and result serialization.
- Preserve the complete assistant tool-call object and append each result as a
  `role: "tool"` message whose `tool_call_id` exactly matches the returned call
  ID. Send the complete structured conversation on the next request and repeat
  until no calls remain.
- Send tool schemas again on every round if the model may call another tool.
  Do not flatten tool calls/results into prose and do not use Gateway
  `session_id` for a tool loop.
- `tool_choice` supports `auto`, `none`, `required`, and one named function.
  `parallel_tool_calls` is supported; clients must be ready to execute every
  returned call or explicitly set it to `false`.
- Codex tool turns use isolated ephemeral app-server threads with local Codex
  tools and configured MCP servers disabled. Treat any Gateway backend error as
  a failed turn; never infer that a requested side effect happened.
- Full request loop, limits, safety contract, and architecture:
  [API_GUIDE.md](API_GUIDE.md#external-tool-call-passthrough) and
  [CODEX_API_TOOL_CALL_BRIDGE.md](CODEX_API_TOOL_CALL_BRIDGE.md).

## Flex Backend Behavior
- Non-HER backend/model switching retains its existing atomic flow.
- HER v2 `/backend` switches only the backend; `/provider`, `/model`, and
  `/effort` are independent persisted controls.
- `/backend +` preserves the handoff intent through that picker and applies it only after the switch succeeds.
- Backend rollback exists: if the new backend fails to initialize, bridge restores the previous backend.
- Flex backend state persists in `workspaces/<agent>/state.json`.
- Persisted state includes:
  - `active_backend`
  - per-backend selected `model`
  - per-backend selected `effort` where supported
- When switching a Codex model, HASHI normalizes an effort unsupported by the
  destination model before it invokes Codex (for example, Sol `max` becomes
  `medium` on Terra or Luna).
- OpenRouter key lookup order for flex agents is:
  - `<agent_name>_openrouter_key`
  - `openrouter-api_key`
  - `openrouter_key`
- Default OpenRouter model is `anthropic/claude-sonnet-4.6`.

## Core Memory Model
- Fixed mode uses real persistent sessions and incremental prompts with
  session-capable Codex, Claude, and Grok CLI backends. Other modes invoke those
  backends as one-shot turns; API, Gemini CLI, and HASHI Engine Runtime (HER) paths remain
  stateless.
- Bridge owns normal context assembly. Optional Memory+ adds a canonical bounded
  today card, short carryover, and archive pointers across every execution mode.
- `/new` starts a fresh CLI session and re-primes the agent with this FYI
  catalog. If Memory+ is enabled, its compact card is preserved and reloaded.
- `/fresh` starts a clean API context. It clears recent turns, preserves saved memories, and disables saved-memory auto-injection until `/memory saved on` or `/memory on` restores it.
- `/handoff` restores recent continuity from bridge transcript, not CLI resume state.
- `/fyi` explicitly refreshes awareness of this bridge environment and can carry a follow-up prompt.
- Memory+ never injects archived prompts or full answers. `/notepad history` and
  `/notepad find <query>` expose bounded archive pointers only when requested.
- Full Memory+ behavior is documented in
  [Memory+ v2 — Compact Work Continuity](MEMORY_PLUS_V2.md).
- `/bg` is an explicit manual background-work entry point. If a `/bg` task needs long shell/process execution, start it through the managed background job path, report the job id and notification/event behavior, and use `/bg status`, `/bg tail`, or `/bg cancel` for follow-up.
- Terminal success/failure notifications can enqueue a `background-job-event` back to the responsible agent. The agent's job is to make the terminal outcome useful to the user: summarize the result, continue the workflow if that is clearly the next responsible step, ask for confirmation when intent is ambiguous, or report failure with the relevant tail excerpt.
- Progress updates during a running job are not yet a built-in manager heartbeat. If a task needs periodic progress messages, the task command must emit them or call an approved notification surface intentionally; normal terminal completion/failure routing remains managed by BackgroundJobManager.

## Skills System
- Skills live under `skills/<kebab-case-name>/SKILL.md` and contain portable instructions plus optional `scripts/`, `references/`, and `assets/` resources.
- `name` and `description` are required Agent Skills frontmatter. Standard optional metadata fields are accepted.
- `/skill` lists, applies, and maintains instruction packages. Per-agent package enable/disable state is lifecycle metadata, not a legacy toggle Skill type.
- Local packages can be validated and copied or linked. Built-in and installed packages can be recoverably deleted, linked packages can be unlinked, and structured Job references block removal.
- Skill detail cards show cumulative project-wide uses. New invocation metadata is appended without prompts to `state/skill_usage.jsonl`, while older direct invocations are recovered from per-agent `token_audit.jsonl` records.
- `/jobs` owns cron, heartbeat, nudge, and deterministic automation. Debug, recall state, and Dream are runtime controls; `/EXP` stays independent.
- Native HER/Claw Skill discovery and execution are disabled. HASHI is the only live Skill owner.

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

## Superloop (Recording-First Long Workflow)

Use `/superloop` for long-running, stateful, multi-step orchestration that must
survive chat/session boundaries and can coordinate across agents.

Core commands:

- `/superloop record start <goal>`
- `/superloop record status [recording_id]`
- `/superloop record try <recording_id> <step title>`
- `/superloop record intent <recording_id> <summary>`
- `/superloop record exit <recording_id> <kind> <details-json>`
- `/superloop record finish [recording_id]`
- `/superloop status <loop_id>`
- `/superloop pause <loop_id>`
- `/superloop resume <loop_id>`
- `/superloop next <loop_id>`
- `/superloop task add <loop_id> <title>`
- `/superloop issue add <loop_id> <title>`
- `/superloop wait add <loop_id> <kind> [deadline-iso]`

Persistence:

- recording state/events: `superloops/recordings/<recording_id>/`
- compiled loop state/taskboard/issues/waits: `superloops/loops/<loop_id>/`

Scheduler integration:

- the scheduler ticks `superloops/` each loop and can auto-satisfy
  `sleep_until` waits by deadline.
- when `resume_policy.on_timeout == "raise_issue"`, timeout waits create an
  issue entry automatically for auditability.

## Browser Tool

Agents can control a real web browser (headless or headed) using Playwright.

Operator shortcut:

```text
/browser
/browser status
/browser 1 <task>  # HASHI standalone/headless browser tools
/browser 2 <task>  # CLI backend native browsing/search where supported
/browser 3 <task>  # Brave Search plus public web fetching
/browser 4 <task>  # logged-in HASHI browser extension bridge
```

`/browser` and `/browser status` show one factual dashboard. Status lights mean
green = confirmed online and usable, yellow = unknown/not checked, red = offline
or misconfigured. HEADLESS is Hashi's built-in route and should be available
when Hashi is running; Brave and extension routes depend on live configuration.

Route 4 acts on the user's real logged-in browser. For destructive actions, submissions, purchases, account changes, or bulk edits, confirm before committing the action.

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

## Local And Global `/sys`

- `/sys <slot> ...` continues to manage the current Agent's
  `workspaces/<agent>/sys_prompts.json`.
- `/sys global <slot> ...` manages the HASHI-instance shared slots;
  `/sys g ...` is the exact short form.
- Shared state lives at `bridge_home/state/global_sys_prompts.json`, refreshes
  on every request, and is never copied into Agent workspaces.
- Active global entries are injected before local entries and win only over a
  conflicting local `/sys` entry; existing higher authority boundaries remain
  unchanged.
- Global changes reach configured Bridge Agents on their next request, including
  queued Scheduler and Automation requests. In-flight requests are unchanged.
- Global activation and deletion require confirmation. Replacing an active
  global slot requires `replace CONFIRM <message>`.
- Global `save` only fills an empty slot; use `replace` for configured slots.
- HER/Ultra internal sub-agents have a separate native prompt path and do not
  directly load Bridge-level Global Sys state.

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
  "tiers": ["core", "desktop"]
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
  "tiers": ["core", "windows_use"]
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
- cross-instance `tools/hchat_send.py` now prefers shared-token Remote protocol
  transport (`/protocol/message`) when available.
- `Remote /hchat` remains a restricted-network legacy fallback for LAN /
  internet relay when protocol transport is unavailable.
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

### Cross-Instance Remote Tools (Messages, Attachments, Files)

For trusted cross-instance delivery on a LAN PC, prefer the dedicated Remote
tools instead of inventing ad-hoc curl flows.

Plain message:

```bash
python tools/protocol_send.py \
  --to agent1@INTEL \
  --from zelda \
  --text "hello from HASHI1" \
  --shared-token "$HASHI_REMOTE_SHARED_TOKEN"
```

Message with attachment:

```bash
python tools/protocol_send.py \
  --to agent1@INTEL \
  --from zelda \
  --text "see attached" \
  --attach ./report.txt \
  --shared-token "$HASHI_REMOTE_SHARED_TOKEN"
```

File push + stat:

```bash
python tools/remote_file_transfer.py \
  --shared-token "$HASHI_REMOTE_SHARED_TOKEN" \
  --from-instance HASHI1 \
  push ./report.txt INTEL:incoming/remote_smoke/report.txt

python tools/remote_file_transfer.py \
  --shared-token "$HASHI_REMOTE_SHARED_TOKEN" \
  --from-instance HASHI1 \
  stat INTEL:incoming/remote_smoke/report.txt
```

Operational rules:

- Cross-instance message targets must use `agent@INSTANCE`.
- Cross-instance file targets must use `INSTANCE:path`.
- `tools/hchat_send.py --to agent@INSTANCE ...` is the correct operator tool for
  real-time cross-instance Hchat delivery.
- `protocol_send.py` requires `--from`.
- Shared-token file transfer requires `--from-instance` unless
  `HASHI_INSTANCE_ID` or `global.instance_id` already defines it.
- On Windows peers, relative paths resolve under that peer's Hashi root.
- Attachment send depends on peer capability `message_attachments_v1`.
- Shared-token file transfer depends on peer capability `file_transfer_hmac_v1`.

Permanent fix note:

- Remote now probes live `/protocol/status` before shared-token file transfer
  and attachment sends, so stale peer metadata fails clearly instead of
  degenerating into ambiguous `401` or `404` errors.
- `hchat_send.py` cross-instance delivery now prefers protocol transport over
  legacy `/hchat`, fixing the case where legacy `/hchat` was bearer-gated while
  trusted protocol traffic already worked via shared-token HMAC.
- Remote also widens local Workbench host fallback beyond `127.0.0.1`.
  This fixes the Windows/LAN case where a peer can receive protocol traffic but
  cannot inject it into its own Workbench because the Workbench is bound to a
  LAN IP instead of loopback.
