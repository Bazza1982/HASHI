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
- `/new`: fresh session start with bridge FYI primer.
- `/handoff`: fresh continuity restore from recent chat history.
- `/fyi [prompt]`: explicit bridge environment awareness refresh.
- `/active [on|off] [minutes]`: toggle proactive follow-up heartbeat; default is 10 minutes.
- `/voice [status|on|off|provider|providers|voices|use <alias>]`: control native bridge-owned voice replies.
- `/retry`: resend last response or rerun last prompt.
- `/debug <prompt>`: strict debug mode with verification-first behavior.
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
- `/new` starts fresh and re-primes the agent with this FYI catalog.
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
- `recall` is a bridge policy toggle: if ON, recent continuity is auto-restored once after an unexpected restart, but not after `/new`.

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
- **跨实例（HASHI2/HASHI9等）：** 通过 Cross-Instance Mailbox 发消息到 HASHI1 的 lily，或使用 `/ask lily 你的问题`

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

## Cross-Instance Mailbox — Inter-Instance Agent Messaging

HASHI instances can send messages to agents on other instances via a shared file-based mailbox. No API or network protocol is needed — all instances share filesystem access.

### Instance Paths

| Instance | Mailbox Path (from WSL) |
|----------|------------------------|
| HASHI1 | `/home/lily/projects/hashi/mailbox/incoming/` |
| HASHI2 | `/home/lily/projects/hashi2/mailbox/incoming/` |
| HASHI9 | `/mnt/c/Users/thene/projects/HASHI/mailbox/incoming/` |

From Windows, use the equivalent UNC/native paths:
- HASHI1: `\\wsl.localhost\Ubuntu-22.04\home\lily\projects\hashi\mailbox\incoming\`
- HASHI2: `\\wsl.localhost\Ubuntu-22.04\home\lily\projects\hashi2\mailbox\incoming\`
- HASHI9: `C:\Users\thene\projects\HASHI\mailbox\incoming\`

### Sending a Message

Write a JSON file to the **target instance's** `mailbox/incoming/` directory.

**Filename format:** `{timestamp}_{from_instance}_{from_agent}.json`
Example: `20260324-053200_HASHI1_hashiko.json`

**Message format:**
```json
{
  "msg_id": "xmsg-20260324-053200-hashiko-akane",
  "from_instance": "HASHI1",
  "from_agent": "hashiko",
  "to_instance": "HASHI9",
  "to_agent": "hashiko",
  "intent": "ask",
  "reply_required": true,
  "text": "Your message here",
  "ts": "2026-03-24T05:32:00Z"
}
```

**Fields:**
- `intent`: `ask` | `inform` | `reply` | `task`
- `reply_required`: if `true`, recipient should reply to sender's mailbox
- For replies, include `reply_to` with the original `msg_id`

### Receiving Messages

Agents check their instance's `mailbox/incoming/` on demand (when told to check, or during `/fyi`). This is manual — no background polling.

**Processing flow:**
1. Read files in `mailbox/incoming/`
2. Move processed messages to `mailbox/done/`
3. If reply needed, write reply JSON to the sender's `mailbox/incoming/`

### Conflict Prevention

- Filenames include timestamp + source instance + agent — guaranteed unique
- Write via temp file + rename for atomicity (prevents reading half-written files)
- Each instance only processes its own `incoming/` — no cross-reading of processing state
