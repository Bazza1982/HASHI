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

## Practical Expectations
- Prefer bridge-owned evidence: code, logs, config, transcripts.
- Use `README.md` when you need deeper detail, or the user has system related questions.
- Do not assume CLI internal session memory is available or reliable.
