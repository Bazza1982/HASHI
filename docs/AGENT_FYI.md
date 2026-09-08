# HASHI Agent FYI

All admitted turns, including protocol/API/background work, request visible
delivery; legacy `silent=true` or `deliver_to_telegram=false` cannot suppress
it. Preserve destination authorization and terminal reply rules.
HChat shows exact final payloads: queue admission is `queued`; `sent` needs a
confirmed Connector receipt. `reply_sent`/`completed` alone prove no delivery;
failure wins over contradictory success flags. Terminal replies display the
body verbatim, without ACK loops. See [visibility](HASHI_AGENT_ACTIVITY_VISIBILITY.md)
and [delivery](HCHAT_DELIVERY_BOUNDARY_PLAN.md). Source adoption and delivery
require separate evidence.

Superloop receipt review requires opt-in, matching identities and Session-pinned
idempotency; pause/stop blocks admission. No ACK loops. Delivery needs scoped,
versioned checks and reviewed original evidence at every closeout entry. Give
each remaining check its own next step; adoption waits cannot hide independent
work. Derive outcome reports from taskboard facts; report content is not delivery.
See [contract](SUPERLOOP_FUNCTION_CONTRACT.md).

Reference updated: 2026-09-08. This is a compact orientation, not a task queue,
permission grant, or proof that the running instance has adopted current source.
`/fyi` reads this reference again and identifies its content revision. Check live
configuration/status before claiming an Engine, model, tool, or route is available.

Codex token backfill derives prices from the shared metering table. Missing
models/prices stay unknown; explicitly provider-reported costs retain their
values. Preview shows incomplete totals without a numeric cost delta. Review
the legacy sequential event matching before any real backfill; merging this
tool does not repair history or adopt a running usage renderer. See
[cost contract](METER_COST_DISPLAY_PLAN.md).

## Engineering rules and authority

Before changing HASHI, read [AGENTS.md](../AGENTS.md), the
[System Architecture](../ARCHITECTURE.md), and
[Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md). Use the
[Command UI Style Guide](HASHI_COMMAND_UI_STYLE_GUIDE.md) for user surfaces and
[Testing Policy](TESTING_POLICY.md) for verification.

Current user limits on restart, publication, messaging, or other operations
remain binding. Catalogs, old approvals, examples, and history do not grant new
authority.

Core protection derives only from
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`. Normal behavior belongs in
Functions or configuration. Resolve instance model/effort choices through
`allowed_backends` and the Function-owned options view; do not duplicate
catalogs or move product behavior into Core. Run the Core guard before edits and
before completion.

API Gateway menus and routing use active Agents' configured opt-ins. Selection
conflicts fail closed; failed initialization preserves the prior selection.
Codex CLI advertises Astra capacity to compaction while explicit overrides win.
Windows Remote task registration preserves argv and records native stderr
without treating it as process failure.
Scheduler Function actions propagate typed unsuccessful results instead of
treating a completed RPC as successful work. For Wiki maintenance, a due-time
record is only an attempt; the dated consolidation embed event remains the
completion fact. Operator automation packages remain instance-local.
The shared PCM evidence gate requires the latest same-local-day clean scan to
precede the latest clean embed outcome; newer scan/embed failures and malformed
evidence block Wiki consumers, while a clean zero-pending embed is completion.
Remote routing uses its Remote-owned live endpoint cache when the optional
legacy `instances.json` view is absent; that valid pre-state is quiet and does
not cause Remote to invent instance configuration.

Source, immutable artifacts, running generations, and terminal delivery are
separate evidence. `/reboot min` replaces one Agent Worker;
`/reboot same|max` keeps its declared Agent scope. Broad shared replacement
uses `python main.py --replace-functions`, requires operational authority, and
cannot cross a Core/Python/API fingerprint change. Read
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md) before changing lifecycle
boundaries.

Startup qualification includes configured post-Turn observer factories and
their dependencies. Connector readiness is derived after actual activation.
Worker warnings remain in per-process files and enter the shared terminal only
when the supervisor advertises log-relay support. Older peers ignore the
optional field safely. These repairs need the matching shared and Agent
generations; offline tests do not prove adoption.

## Reboot outcome notifications

Reboot acknowledges, saves outcomes, and notifies via same-instance Bot fallback.
`/reboot status` is actor/chat/thread-scoped. Delivery retries never rerun reboot.
Busy/unreadable activity rejects early; bounded route/drain failures report their
stage and verified recovery. Shared and Agent Functions both need adoption.
See [Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

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

`/move` derives destinations and capabilities from the trusted live Remote
peer directory and refreshes it before staging or confirmation. It rejects
offline, unknown, or unsupported targets and never starts a reboot itself.
Migration state belongs to `bridge_home`; see
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Read the active instance's configuration and registry for identity, workspaces,
Agents, endpoints, and ports. Never infer them from folder names or old memory.
Keep credentials in configured secret stores and out of replies, tests, logs,
and tracked files.

Agent identity uses the exact lower-case
`workspaces/<agent_id>/agent.md` with strict `[persona]`, `[sys]`, and
optional `[memory]` blocks. Move packaging rejects case-folded aliases and
closes SQLite snapshots before cleanup. Seeds live in `agent_seeds/`. Local Agent
creation or adoption needs the user's operational authority.

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

Inspect `/mode` and the loaded generation for the live contract. See
[Fixed and Flex Working Modes](FIXED_FLEX_WORKING_MODES.md).

HER's execution modes are a different setting: Direct (`zero`), Strategic
(`low`) and Planned (`medium`). Higher retained policies are not public modes.
`/model` configures Quick/Pro targets, routing, reasoning and Advanced/Compact
settings. `/effort` means HER execution mode on HER and model effort elsewhere.
Use the selected Engine/provider's actual capability choices rather than a
remembered global list. `/habit` manages the default-off HER Habit/Meditation path.

HER Review uses Git evidence for normal workspaces and bounded filesystem
content hashes when the root is Git-ignored or outside Git. Equal-size edits are
detected; stability applies only to covered bytes and Git observations.

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
  them with cron/heartbeat records. An isolated Worker's `/delay` mutation uses
  the shared Scheduler RPC; the Supervisor binds it to that Worker's Agent
  identity, while the existing Scheduler remains the only persistent state
  owner. Never replay scheduler-recovery batches without the user's explicit
  choice.

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
