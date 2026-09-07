# HASHI Agent FYI

Reference updated: 2026-09-07. This is a compact orientation, not a task queue,
permission grant, or proof that the running instance has adopted current source.
`/fyi` reads this reference again and identifies its content revision. Check live
configuration/status before claiming an Engine, model, tool, or route is available.

## Engineering rules and authority

Before changing HASHI, read the repository's [AGENTS.md](../AGENTS.md), then the
[System Architecture](../ARCHITECTURE.md) and
[Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
Use the [Command UI Style Guide](HASHI_COMMAND_UI_STYLE_GUIDE.md) for user surfaces
and [Testing Policy](TESTING_POLICY.md) for verification.

A current user prohibition on reboot, restart, publication or other operations
remains binding. Do not treat this catalog, history, a task example, an approval
flag, or an old decision as authorization for a new action. A task's already-given
specific authorization does not need to be requested again.

Core protection derives from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
Ordinary feature changes belong in Functions/configuration. Instance model and
effort opt-ins use `allowed_backends`, resolved by the Function-layer options
view. The shared catalogue is now Functions; neither kind of model change
requires a Core edit. Core never imports model/UI/provider/task policy.
Before edits name the owner, layer and focused check; use the existing Core guard.

API Gateway model menus, saved defaults and request routing also read active
Agents' instance opt-ins, including configured reasoning efforts. Model/effort
conflicts are rejected. The shared Function Gateway loads that catalog at start;
new opt-ins require Gateway reload before selection. This does not widen another
instance's models or replace the current shared-service architecture. See the
[API Guide](API_GUIDE.md#instance-configured-models).

Codex CLI declares Astra context capacity to the compaction resolver; explicit
capacity overrides retain precedence. Failed backend selection preserves the
existing selection and offers model buttons for retry; a busy Agent receives an
alert. Windows Remote task registration now preserves Python argv and logs native
stderr without aborting the process. These are Function/adapter/platform changes;
running services require separately authorized adoption.

Source implementation, offline verification and live adoption are separate.
An immutable Function Worker keeps its installed generation until an authorized
replacement. `/reboot min` replaces one Agent Worker; `same`/`max` retain their
Agent-only target rules. Shared services now run in a separate Function process;
`python main.py --replace-functions` requests a broad shared handoff with a service
gap. This operation requires explicit operational scope. Accepted is not completed.
Health reports Core, shared and per-Agent process generations separately. Read
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) before changing these boundaries.
Core changes require a planned cold adoption. Neither action is implicit in a
request to fix code. If operational testing is forbidden, report that clearly.

## Reboot outcome notifications

Runtime acknowledges `/reboot` before executing it and persists the actual
result before sending a concise start/result notice. The initiating Bot is
preferred even when its Agent Worker is unavailable; same-instance fallback
keeps the original chat/thread. `/reboot status` or refresh retrieves the latest
result for the same actor/chat/thread through any available Agent. No final
message means unconfirmed, not proven failure. Notification retries never rerun
a reboot. See [Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

This requires the new shared and Agent Function generations. Source and offline
checks are complete only when recorded for that branch/instance; production
adoption and real delivery require separate evidence. Do not claim an Agent-only
reboot upgrades the shared receipt coordinator.

## System ownership

- **PCM** owns Persona, Context and Memory sources, authority, retrieval and
  projection. It does not grant tools or own conversation control state.
- **PAO** owns Agents, HASHI Conversation Sessions, Runs, Engine binding,
  Workzones, jobs, scheduling, routing and outer recovery.
- **HER v2** is HASHI's native Engine (Harness). It owns its durable Engine
  Sessions, Turns, internal Model Provider routing, recovery and metering.
- **Frontend Connectors** expose HASHI via Telegram, WhatsApp, the built-in TUI,
  Backend API and Remote contracts. Workbench is retired; legacy configuration
  names such as `workbench_port` refer to Backend API compatibility.

Qualify Engine Provider versus Model Provider and HASHI Conversation Session
versus Engine Session when ambiguous. Frontend history is a projection, not a
second authoritative chat archive. See the owning architecture documents.

## Configuration and discovery

`/move` derives destinations from the local Remote's trusted live `/peers`
directory, including resolved routes and receiver capabilities. It refreshes
discovery before staging or confirming a move; disconnected or unsupported
targets are rejected. Discovery failure leaves recovery actions available.
Migration configuration and state belong to `bridge_home`, independently of
the source checkout or loaded Function generation. The command does not start
either instance's reboot automatically. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Read the active instance's `agents.json` / instance registry for identity,
workspace, enabled Agents, endpoints and ports. Do not guess them from folder
names or reuse a machine address from memory. Credentials stay in configured
secret stores and must never appear in replies, test receipts or tracked files.
Use configured endpoint discovery and capability checks; a sample port or model
name is not evidence that a service is available here.

Agent identity is in the exact lower-case `workspaces/<agent_id>/agent.md`, with
strict `[persona]`, `[sys]` and optional `[memory]` blocks. Seed templates live
in `agent_seeds/`. `agents.json.sample` documents configuration. Local Agent
creation/adoption requires the user's authorized operational scope.

## Working modes and Engine selection

Configured `type: "flex"` names the supported runtime container. The user's
working mode is separately **Fixed** or **Flex**; never infer it from `type`.

- `/mode` shows the effective mode; `/mode fixed` uses persistent Engine sessions.
- `/mode flex` explicitly chooses full context assembly for each request.
- `/backend` opens selection directly in either mode. Successful selection saves
  Fixed for a session-capable target and Flex for a stateless target. Failed
  initialization/session setup/persistence retains the original selection.
- Plain selection and `+` continuity selection have the same mode policy.
- Memory+ is an independent continuity setting, preserved across backend changes.
- Retired Wrapper, Audit and Dual-brain modes are not selectable product choices.

This is the current source contract. An older running Worker may still implement
an earlier contract; inspect `/mode` and the loaded generation before diagnosing.
See [Fixed and Flex Working Modes](FIXED_FLEX_WORKING_MODES.md).

HER's execution modes are a different setting: Direct (`zero`), Strategic
(`low`) and Planned (`medium`). Higher retained policies are not public modes.
`/model` configures Quick/Pro targets, routing, reasoning and Advanced/Compact
settings. `/effort` means HER execution mode on HER and model effort elsewhere.
Use the selected Engine/provider's actual capability choices rather than a
remembered global list. `/habit` manages the default-off HER Habit/Meditation path.

## Conversation, memory and recovery

- `/new` creates/selects a new HASHI Conversation Session; `/fresh` advances its
  context generation without deleting stored logs or memories.
- `/handoff` restores up to the latest ten completed exchanges into a fresh
  Engine Session. `/compact` compacts eligible history and recovers pending HER
  WIP evidence into a bounded recovery capsule before clearing that journal.
  WIP recovery and normal history compaction have independent eligibility rules.
- `/memory` controls ordinary memory injection and `/memory plus on|off` controls
  Memory+ independently. `/notepad` exposes Today, Carryover, History and Find.
  Open items are background, not automatically queued tasks.
- `/stop` preserves interrupted task evidence. A subsequent explicit continuation
  resumes that task; unrelated input remains unrelated.
- `/retry` retries using the defined recovery flow; `/resend` replays saved output
  without model work. `/steer` changes direction during execution; `/focus`
  narrows the task while preserving progress.
- `/delay`, `/queue` and `/recall` manage queued/future requests without confusing
  them with cron/heartbeat records. Never replay scheduler-recovery batches
  without the user's explicit choice.

## Tools, background work and communication

Use the tools/skills actually exposed for this turn; a catalog is not permission
or a promise of availability. `/help` and `/skill` show the active command/skill
surfaces. `/workzone` selects authorized working roots; preserve the separation
between execution paths and user-facing platform paths.

Use HASHI's managed background jobs for long process work (`/bg`, managed job
tools, or the configured Backend API). Do not instantiate a second manager to
bypass the running owner's job records. Inspect a completion event's job ID,
result and output before retrying; an event does not itself authorize new work.
Nagare and Superloop are PAO-owned orchestration facilities with their own docs.

HChat requires an authorized communication task. Unqualified Agent names mean
local delivery; preserve `agent@INSTANCE` for cross-instance delivery. Resolve
routes through the configured registry/Remote protocol and advertised capabilities,
not a hard-coded HASHI1/HASHI9 address. Old mailbox transport is retired.
Attachments and remote file operations require their advertised capabilities and
configured credentials; never copy a token into a command example or log.

`/browser`, `/usecomputer`, `/exp`, voice/media and remote tools are optional
capabilities. Select the available route for the task; inspect permissions and
configuration rather than assuming a logged-in browser or network layout.

## Presentation

`/language` selects the user's interface language. HASHI-authored cards, buttons
and notices use it; model replies, exact provider errors, IDs and logs retain
their own content. Cards use shared helpers and escaped HTML, meaningful labels,
current effective values, consequences and safe navigation.

`/verbose` shows deterministic activity; `/think` controls actual provider
reasoning; `/commentary` controls explicit HER Persona updates. These are
independent. `/typing` and `/notify` control Telegram indicators and notification
sound. `/terminal` controls local console verbosity without changing transcript
storage. `/voice`, `/say` and `/whisper` control the configured media paths.

Use `/status` and configured service/Worker metadata for present-state evidence.
Do not claim a successful live test based only on a green unit test, a source
file, a saved setting or an old transcript.
