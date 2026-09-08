# HASHI Agent FYI

Every admitted HChat/protocol, API, bridge or background turn requests visible
delivery; `silent` and legacy false delivery flags cannot hide it. `queued` is
not `sent`: only a Frontend Connector receipt proves transport, and errors stay
failed. Terminal protocol replies return one verbatim body without ACK loops.
See the [visibility decision](HASHI_AGENT_ACTIVITY_VISIBILITY.md) and
[HChat delivery decision](HCHAT_DELIVERY_BOUNDARY_PLAN.md).

Superloop receipt review is explicit opt-in. It requires a matching active
dispatch, task/controller identity, local evidence and Session-pinned
idempotency; pause/stop blocks it. A queued receipt proves no review, merge,
adoption or delivery. See the [receipt review contract](SUPERLOOP_PLAN.md#correlated-receipt-review-admission-2026-09-08).

Reference updated: 2026-09-08. This is a compact orientation, not a task queue,
permission grant, or proof that the running instance has adopted current source.
`/fyi` reads this reference again and identifies its content revision. Check live
configuration/status before claiming an Engine, model, tool, or route is available.

## Engineering rules and authority

Before changes, read [AGENTS.md](../AGENTS.md), the
[architecture](../ARCHITECTURE.md), and
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md); apply the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md) and
[testing policy](TESTING_POLICY.md) when relevant.

A current prohibition on reboot, restart or publication remains binding. This
reference and old approvals grant no new authority; an exact existing approval
need not be requested twice.

Core protection derives only from `CORE_SOURCE_PATHS`; normal behavior belongs
in Functions/configuration and Core imports no product policy. Name owner,
layer and focused check before edits and run the guard. Instance model/effort
opt-ins use `allowed_backends` plus the Function resolver; shared compatibility
uses its Function catalogue, not a Core edit.

API Gateway menus/defaults/routing read active instance opt-ins and reject
model/effort conflicts. Its shared Function loads that view at start; see the
[API Guide](API_GUIDE.md#instance-configured-models).

Codex Astra capacity feeds compaction unless explicitly overridden. Failed
backend selection preserves the old choice; busy Agents reject switching.
Windows Remote registration preserves Python argv and native stderr. These
Function/platform changes still need separately authorized adoption.

Source, offline checks and live adoption are separate. `/reboot min` replaces
one immutable Agent Worker; `same`/`max` keep Agent scope. Shared replacement
(`python main.py --replace-functions`) is broader, has a service gap and needs
explicit scope. Health reports Core/shared/Agent generations separately.
Core changes require planned cold adoption. See
[Minimal Core](HASHI_SLIM_CORE_ARCHITECTURE.md).

A configured CLI Worker probe may consume same-instance
`state/service_endpoints.json` via `--service-endpoints`; it republishes only
inside the isolated probe. READY proves construction, not shared health,
activation, inference or adoption.

Startup qualification includes configured observer factories/dependencies and
invalidates incomplete caches. Readiness follows actual Connector activation.
Worker warnings always stay in per-process logs and relay only when the
Supervisor advertises the optional capability; missing/malformed capabilities
fail closed, preserving old-Supervisor READY compatibility. See
[Startup and qualification ownership](HASHI_SLIM_CORE_ARCHITECTURE.md#startup-presentation-and-connector-health).

## Reboot outcome notifications

`/reboot` persists its outcome and sends start/result notices. `/reboot status`
reads the latest receipt for the same actor/chat/thread; missing delivery is
unconfirmed, not failure, and notification retry never reruns a reboot. Shared
receipt coordination and Agent Worker adoption remain separate. See
[Reboot Receipts](HASHI_REBOOT_RECEIPTS.md).

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

The 2026-09-08 migration review found the HASHI1 delivery patch already matches
shared main's product behavior. The shared checkpoint retains only stronger
dry-run and rollback tests. HASHI1 runtime adoption and terminal acceptance
remain unverified; see [Agent Move review evidence](HASHI_AGENT_MOVE_V1.md#shared-review-checkpoint--2026-09-08).

The HASHI1 unified release preflight is recorded separately in
[HASHI1 release preflight](HASHI1_RELEASE_PREFLIGHT_2026-09-08.md). Its shared
candidate changes Core/Function API 2 to 3, so it requires an explicitly
authorized planned cold migration; an Agent `/reboot` cannot adopt it.

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

Use only tools/skills exposed for the turn; a catalog grants no authority.
`/help` and `/skill` show active surfaces, and `/workzone` selects authorized
roots. Keep execution and user-facing platform paths distinct.

Use HASHI's managed background jobs for long work; never create a second manager
to bypass its records. Inspect job ID/result/output before retrying: completion
events grant no new authority. Nagare and Superloop are PAO-owned facilities.

HChat requires an authorized communication task. Unqualified Agent names mean
local delivery; preserve `agent@INSTANCE` for cross-instance delivery. Resolve
routes through the configured registry/Remote protocol and advertised capabilities,
not a hard-coded HASHI1/HASHI9 address. Old mailbox transport is retired.
Attachments and remote file operations require their advertised capabilities and
configured credentials; never copy a token into a command example or log.
Function Worker diagnostic logs redact credential-shaped text, including tokens
embedded in request URLs, before file persistence or shared-console relay.

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
