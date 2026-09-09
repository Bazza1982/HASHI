# HASHI Agent FYI

Every admitted turn, including protocol/API/background work, requires visible
delivery; legacy suppression flags cannot override it. The sole narrow
projection exception is a validated, client-bound TUI
`hashi.frontend-delivery` version 1 policy snapshotted for one Run. It may skip
that Run's Telegram mirror but never its formal Conversation record or TUI
result. Preserve destination authorization and terminal-reply rules. HChat exposes exact final payloads:
admission is `queued`, while `sent` requires a confirmed Connector receipt.
`reply_sent`/`completed` alone are not delivery proof, and failure wins over
contradictory success flags. Terminal replies are verbatim and never create ACK
loops. Source adoption and delivery need separate evidence; see
[visibility](HASHI_AGENT_ACTIVITY_VISIBILITY.md) and
[delivery](HCHAT_DELIVERY_BOUNDARY_PLAN.md).

Superloop receipt review requires opt-in, matching identities and Session-pinned
idempotency; pause/stop blocks admission. Closeout requires scoped, versioned
checks and reviewed original evidence. Keep an independent next step for every
remaining check. Reports derive from taskboard facts but are not delivery. See
the [contract](SUPERLOOP_FUNCTION_CONTRACT.md).

Reference updated: 2026-09-09. This orientation is neither a task queue,
permission grant, nor live-adoption proof. `/fyi` reloads it and identifies the
revision. Verify live status before claiming any Engine, model, tool, or route.

Release naming follows the accepted [versioning policy](HASHI_VERSIONING_POLICY.md)
and [release checklist](RELEASE_CHECKLIST.md). This is a prospective governance
decision recorded in HASHI1, not a bump of the current candidate or proof of
Portable updater support. Release labels, compatibility evidence, exact build
identity, and running-generation adoption remain separate facts.

Codex token backfill uses the shared metering table. Missing prices remain
unknown and provider-reported costs are retained. Preview marks incomplete
totals without inventing a delta. Review legacy event matching before real
backfill; merging the tool repairs neither history nor live renderers. See the
[cost contract](METER_COST_DISPLAY_PLAN.md).

Portable Windows inherits shared runtime policy. Tracked inputs include
`__main__.py`, `runtime-entry.json`, `pyproject.toml`, the lock and protected-Core
manifest. The builder byte-verifies the Function profile; bundled Python validates
runtime and the packaged adapter subset against the shared registry. Release needs
a clean commit/tree; source alone is not a bundle, install or adoption. See the
[builder](../packaging/portable_windows/README.md).

## Engineering rules and authority

Before changing HASHI, read [AGENTS.md](../AGENTS.md),
[Architecture](../ARCHITECTURE.md), and
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md). Follow the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md) and [test policy](TESTING_POLICY.md).

Current user limits on restart, publication, messaging and other operations
remain binding; catalogs, examples, history and old approvals grant no authority.

Core protection derives only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
Normal behavior belongs in Functions or configuration. Resolve model/effort via
`allowed_backends` and the Function-owned options view; never duplicate catalogs
or put product behavior in Core. Run its guard before edits and completion.

API Gateway menus and routing use active Agents' configured opt-ins. Selection
conflicts fail closed; failed initialization preserves the prior selection.
Codex CLI advertises Astra capacity to compaction while explicit overrides win.
Windows Remote task registration preserves argv and records native stderr
without treating it as process failure.
Scheduler Function actions propagate typed unsuccessful results instead of
treating a completed RPC as successful work. For Wiki maintenance, a due-time
record is only an attempt; the dated consolidation embed event remains the
completion fact. Operator automation packages remain instance-local.
The PCM evidence gate requires today's latest clean scan before the latest clean
embed. Newer failures or malformed evidence block Wiki consumers; a clean
zero-pending embed completes the work.
Remote routing uses its Remote-owned live endpoint cache when the optional
legacy `instances.json` view is absent; that valid pre-state is quiet and does
not cause Remote to invent instance configuration.

Source, immutable artifacts, running generations and terminal delivery are
separate evidence. `/reboot min` replaces one Agent Worker; `/reboot same|max`
keeps its declared scope. Shared replacement uses
`python main.py --replace-functions`, needs authority, and cannot cross a
Core/Python/API fingerprint change. See [Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md).

Startup qualification covers configured post-Turn observers and dependencies;
Connector readiness follows activation. Worker warnings stay per-process and
reach the shared terminal only with advertised log relay. Older peers safely
ignore that optional field. Matching shared and Agent generations—not offline
tests—prove adoption.

## Reboot outcome notifications

Reboot acknowledges, saves outcomes and notifies through a same-instance Bot
fallback. `/reboot status` is actor/chat/thread-scoped; delivery retries never
rerun reboot. Busy or unreadable activity rejects early. Route/drain failures
report their stage and verified recovery. Both Function scopes need adoption.
See [Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

## System ownership

- **PCM** owns Persona, Context and Memory sources, authority, retrieval and
  projection—not tools or conversation control.
- **PAO** owns Agents, HASHI Conversation Sessions, Runs, Engine binding,
  Workzones, jobs, scheduling, routing and outer recovery.
- **HER v2**, HASHI's native Engine, owns durable Engine Sessions and Turns,
  internal Model Provider routing, recovery and metering.
- **Frontend Connectors** expose Telegram, WhatsApp, TUI, Backend API and Remote.
  Workbench is retired; names such as `workbench_port` are API compatibility.

The built-in TUI completes entered slash-command prefixes from its displayed
canonical matches and rejects unknown slash commands locally. Its sent/received
sounds, language/layout, typing indicator and default Telegram mirror choice are
local Connector presentation state; they do not alter a HASHI Conversation
Session. Unified command `messages` render immediately and are not transcript
entries. Mouse selection pauses follow-tail; `Ctrl+C` copies the selection to
the Windows clipboard and `Esc` clears it.

`/telegram` and `/telegram on|off` inspect or change the current TUI's default;
`/tui telegram on|off` is the explicit alias. The setting is snapshotted at
submission for future TUI Runs only. Off means zero Telegram typing,
commentary, final or error projection for that Run; it does not disconnect the
Bot, create a private Session, affect another source/window, or cause later
replay. `/tui typing on|off` controls only the local Run-fenced TUI indicator
and is independent of Telegram `/typing`.

The TUI connection footer uses live runtime `presentation_status`; offline
unknown values remain unknown. Model Provider and structured Quick/Pro routing
appear only for HER v2. It intentionally omits working mode. Use `/mode` when
the current mode is relevant. Source presence is not live adoption proof;
confirm the active Function generation/provenance on each instance.

Distinguish Engine from Model Provider and HASHI Conversation from Engine
Session. Frontend history is a projection, not another authoritative archive.

## Configuration and discovery

`/move` is migration only; `/clone` is clone only. Both refresh trusted live
Remote peers before staging/confirmation and use the same authenticated package,
journal, registry, workspace, Scheduler, secret and lifecycle writers. Omitted
clone target means this instance; a supplied target must resolve uniquely, and
`local` is an ordinary possible instance name. ID conflicts use the first free
`_1`, `_2`, … suffix unless a legal free `--as` ID is supplied.

Every instance keeps at least one active Agent. Move therefore rejects the last
active source before prepare. Its target stays inactive until source
Worker/Telegram ingress has stopped; after target activation and Workbench
verification, source registry/workspace/Agent secrets are deleted. Clone leaves
the source active, never copies Telegram credentials, activates the new Agent
for Workbench/API, and imports Scheduler entries disabled. HChat directories use
live `agent_id@instance_id`; an old address returns `agent_moved` plus the new
address from the audit-only tombstone. Historical move journals remain
recoverable. Transfer state belongs to `bridge_home`; see
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Read active configuration/registry for identities, workspaces, Agents, endpoints
and ports; never infer them from paths or memory. Keep credentials only in
configured stores and out of replies, tests, logs and tracked files.

Agent identity is the exact lower-case `workspaces/<agent_id>/agent.md`, with
strict `[persona]`, `[sys]` and optional `[memory]` blocks. Move rejects
case-folded aliases and closes SQLite snapshots before cleanup. Seeds live in
`agent_seeds/`; local creation/adoption requires operational authority.

## Working modes and Engine selection

Configured `type: "flex"` names a runtime container, not the separately selected
**Fixed** or **Flex** working mode.

- `/mode` shows it; Fixed uses persistent Engine sessions, while Flex assembles
  full context per request.
- `/backend` works in either mode. Success chooses Fixed for session-capable and
  Flex for stateless targets; any setup/persistence failure preserves the old
  selection. Plain and `+` continuity selection share this policy.
- Memory+ is independent and survives backend changes. Retired Wrapper, Audit
  and Dual-brain modes are not choices.

Inspect `/mode` and loaded generation for the live contract; see
[working modes](FIXED_FLEX_WORKING_MODES.md).

HER execution is separate: Direct (`zero`), Strategic (`low`) or Planned
(`medium`); higher retained policies are not public. `/model` configures
Quick/Pro routing, reasoning and Advanced/Compact. `/effort` means HER mode on
HER and model effort elsewhere. Use live provider capabilities, not a remembered
global list. `/habit` manages the default-off Habit/Meditation path.

HER Review uses Git evidence normally and bounded content hashes for ignored or
non-Git roots. Equal-size edits are detected; stability covers only observed data.

HER Provider adapters validate a complete tool-call batch before side effects.
Malformed arguments stay in the same tool loop for at most three explicit
format-repair requests; completed tools are not replayed. Full raw wire and
assembly evidence is written only to a restricted local forensic file, while
normal errors expose a safe summary and its path. Explicit Adapter failure
codes survive HER unchanged; this mechanism never reruns a PAO or Cron Run.

## Conversation, memory and recovery

- `/new` creates/selects a HASHI Conversation Session; `/fresh` advances context
  generation without deleting logs or memories.
- `/handoff` restores up to ten completed exchanges into a fresh Engine Session.
  `/compact` compacts eligible history and capsules pending HER WIP before
  clearing its journal; the two eligibility rules are independent.
- `/memory` and `/memory plus on|off` independently control ordinary memory and
  Memory+. `/notepad` exposes Today, Carryover, History and Find; open items are
  background, not queued tasks.
- `/stop` preserves interrupted evidence. Explicit continuation resumes it;
  unrelated input stays unrelated.
- `/retry` uses defined recovery; `/resend` replays output without model work.
  `/steer` redirects execution; `/focus` narrows it while preserving progress.
- `/delay`, `/queue` and `/recall` manage requests, not cron/heartbeat records.
  Isolated Workers mutate delay through Scheduler RPC bound by the Supervisor to
  their Agent; Scheduler alone owns persistence. Never replay recovery batches
  without the user's explicit choice.

## Tools, background work and communication

Use only tools and skills exposed for the current turn. `/help`, `/skill`,
and `/workzone` show current surfaces and authorized roots.

Use HASHI-managed jobs for long processes; do not create a second manager.
Inspect the recorded job result before retrying. Nagare and Superloop are
PAO-owned facilities with separate contracts.

HChat needs an authorized communication task. Unqualified Agent names are local;
retain `agent@INSTANCE` for cross-instance routing. Resolve routes and
capabilities through Remote. Old mailbox transport is retired. Never put
credentials in commands, logs, or receipts; Worker logs redact
credential-shaped text before persistence or relay.

Browser, computer, EXP, voice/media, and Remote tools are optional. Check current
capability and configuration before using them.

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
Worker busy/queue views derive from runtime metadata republished on admission,
start, generation end and cleanup, including failure/cancellation. Request
activity supplies detailed events without becoming another status owner.
Do not claim a successful live test based only on a green unit test, a source
file, a saved setting or an old transcript.

Usage summaries preserve two distinct facts: `cost_usd` is the known subtotal,
while `unknown_cost_requests` counts requests without a cost. `/usage`, `/token`
and `/status` must use the shared formatter: show “cost unknown” when every cost
is unknown, or a known subtotal plus the missing-request count when only part is
known. A zero with no unknown requests remains an exact provider/local zero.
Never turn an unknown or partial cost into a complete `$0.0000` total.

## Superloop controller follow-through

PAO rechecks the latest receipt against the whole board; recovery is once only.
Failed/cancelled/exhausted Runs need existing supervisor/heartbeat attention.
`continuous_supervision_required` requires action `next` and dispatch/wait `review_after`.
Execution never proves delivery. Inspect exact-request Connector evidence; lead
reports with user outcomes and next owner/action. No new Core or cron authority.
See `SUPERLOOP_FUNCTION_CONTRACT.md` for delivery opt-ins and adoption boundaries.
