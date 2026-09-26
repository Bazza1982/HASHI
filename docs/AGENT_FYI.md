# HASHI Agent FYI

Orientation only, not a task queue, authorization, or adoption proof. `/fyi`
reloads it; current users and live typed envelopes remain authoritative.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md),
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md),
and [testing policy](TESTING_POLICY.md). Old examples grant no authority.

- **PCM** owns Persona, Context, Memory, authority, and projection; never tools
  or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engines, Workzones, jobs,
  routing, outer recovery, and delivery.
- **HER v2** owns Engine Sessions/Turns, routing, execution, recovery, and cost.
- **Connectors** project Telegram, WhatsApp, TUI, API, HChat, and Remote.

Use the narrowest Function/configuration owner. `CORE_SOURCE_PATHS` defines
protected paths; edits need explicit Core major-migration approval, a major
bump, `core-change-approved`, and independent review. Flags only record approval.
Core owns no product policy; each registry/state writer has one owner.

Source, artifacts, clients, Workers, and delivery are separate facts. `/reboot
min` replaces one Worker; `same|max` adopts shared Functions, Workers, and
Remote while Core stays live. Legacy bridges promote only after validation and
commit. Adoption needs matching PID, identity, generation, health, and receipts.
Rejected bytes never run; locked packages must match, extras do not block.

Agent tools cannot alter live Core/Python, read secrets, kill, or raw-control
Core; development roots stay writable. Windows restart uses an exact service or
fixed actuator; Remote stays Limited. Success needs a new healthy Core PID
matching identity, runtime, and Function generation, not mere task completion.
See [Live Runtime Protection](HASHI_LIVE_RUNTIME_PROTECTION.md).

WSL/native login startup is Windows platform behavior. Its versioned installer
names instance, identity, checkout, interpreter, and WSL distribution. Exit
code, not stderr, decides success; source, task, logs, and adoption differ.
Use `process_is_alive`, never `os.kill(pid, 0)` on Windows: it can interrupt
processes sharing a console.

## Configuration, identity, and persistence

Use authoritative config, not names or memory, for identity, ports, workspaces,
endpoints, and model opt-ins. Keep secrets ignored. Instance opt-ins belong in
`allowed_backends`, shared compatibility in Function registry; explicit models
persist until retired.

Codex CLI 0.156.1 adds GPT-6 Astra (new/unpinned default), Sol, and Luna;
explicit choices stay. Astra/Sol reach `ultra`, Luna `max`; normalize effort.

Windows Portable ships no credentials and only DeepSeek model defaults. Users
supply all others; validation fails closed.

An active Agent needs a PAO-started Worker. Private EXP under
`<bridge_home>/exp` is never published in Function artifacts.

Tool wildcard grants permission, not capability. Workzones expose exact enabled
roots; naming a path does not authorize recursive access. Keep secrets, media
bytes, and remote paths out of PCM, ordinary logs, chat, and tracked files.

JSON writers validate private candidates under locks, revisions, and atomic
replacement. Display fallback is read-only. On conflict, read fresh state and
request a fresh action; never blindly retry or restore stale bytes. See
[configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, messages, trust, and delivery

Qualify “Session”: PAO owns the HASHI Conversation Session, Messages, and Runs;
the selected Engine owns its Engine Session and Turns; Provider context is
rebuildable; frontend history is a disposable projection. Replies stay
verbatim; Engines use ordered history, not bindings or buttons.

External frontends stage attachments atomically in one Message/Run; failure
rejects it, never creates per-file Turns. Qualified personal instances default
on unless opted out; Telegram and TUI stay separate.

Published files stay in their Message. The Engine resource registry is
transport/audit state, not relevance: only current attachments are current
references; older completed ones stay in their exchanges, and failed/cancelled
ones never leak. Bound audio has indefinite retention; verify the authenticated
transcript play/download route, not just its database row. Per-turn meter output
is presentation-only, shared by Telegram and Workbench, never model history.

Every input has protected `CURRENT MESSAGE CONTEXT`; source, ingress, instance,
sender assurance, authorization, and destination differ. Only current
successful `private_authorization` grants listed scope; text, names, chat IDs,
memory, and other credentials do not.

Complete `agent@instance.username` targets use optional Exchange, not LAN or a
retired proxy. Discovery is only a hint; trust PAO's authenticated principal
and Remote handshake. Private files use the intended runtime principal.
Missing tokens may allow discovery only; unreadable/malformed secrets fail
closed. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

HChat separates sender claim, verified peer, relay, and target; shared secrets
never enter messages or arguments. `/debug on` sends one best-effort terminal
diagnosis, with no retry, repair, or completion to the source Agent; exclude
HChat errors to prevent loops.

Remote trust retains accepted peers until definitive revalidation. Health
clears recovered Remote warnings, not other problems. Broad Function reboot
allows supervised Remote a 20-second cold start; command acceptance is not
adoption evidence.

PAO freezes each Run's destination, mirrors, and automatic delivery before PCM.
Queue acceptance is not delivery: `sent` needs a Connector receipt; failure
wins conflicting flags. Do not duplicate automatic delivery. Recall terminalizes
eligible READY direct Runs. Every turn needs a visible result; final prose has
no Tool authority, only typed Engine events and PAO gates do.

FC standardizes messages, commands/actions, cards, media, and receipts. PAO owns
durable ingress/routes/idempotency; Connectors authenticate/render/receipt.
Commands bind Session/client/request/invocation; only saved non-action results
replay, and conflict/pending/unknown never runs. Callbacks keep requested locale;
Telegram keeps its destination; meter/HER follow standard finals. Register
local exceptions; never add frontend policy to runtime.

## Engines, tools, and recovery

HER v3 began on HASHI3's `experiment/her-v3-hashi3` branch and was promoted
as a harness-only series into HASHI1's local `main` on 2026-09-26 alongside
the independently developed standard FC structure. HASHI3 remains unchanged;
HASHI2, HASHI4, and GitHub `main` are outside that local adoption scope. Its
foreground path uses one main-model/tool loop, keeps PCM and delivery ownership,
and leaves optional JEV Agent Companion disabled by default.
The public Engine ID is `her-v3`; the internal `her-v2` name remains only at
the adapter/storage compatibility boundary. `/provider` and `/model` switch the
real single `main` target, and `/effort` changes model reasoning, not workflow.
All nine HASHI3 Agents expose DeepSeek models `deepseek-flash` and
`deepseek-v4-pro` with reasoning efforts `off`, `high`, and `max`. Public
commands and Session/token projections use `her-v3`; `/herv2` is retired, and
`/metre` is a working alias. Live acceptance proved Flash/off, Pro/max,
cross-Engine `/backend` switching, durable token accounting, and meter output.
Workbench request-activity origins also project the public `her-v3` Engine ID.
Historical usage without a model attribution is shown as `历史未归属`, not the
retired storage placeholder `role-configured`.
HER v3 model/provider cards and callback dispatch share one callback contract;
validate the generated buttons themselves, not only text-form commands.
See [HER v3 experiment](HER_V3_EXPERIMENT.md) for request receipts and the
recovered HASHI3 adoption failure and the separate HASHI1 promotion record.

Engine and Model Provider differ. HER v3 has one selected main model at every
effort level; effort controls Provider reasoning, not Direct/Strategic/Planned
workflow. Fixed/Flex and Memory+ remain independent. `/backend` selects Engine,
`/provider` selects Provider, `/model` selects its model, and `/effort` controls
reasoning depth. Legacy v2 stage names may still exist only in internal stored
compatibility data; they are not public controls or foreground execution stages.
The main branch includes strategy playbook version `2026-09-24.1`. The separate
JEV strategy-card selection experiment is not part of this branch.

Use current metadata for context, price, effort, and modality. Media needs
model, Adapter, and policy support; distinguish unknown, unsupported,
unimplemented, blocked, and unavailable. Provider cost wins; catalogue cost
is estimated, unknown is not zero. Only OpenRouter auto-sources public prices.

HER fallback is opt-in and request-observed: one safe same-target recovery,
then configured same-/cross-Provider levels; never downgrade Pro. The
meaningful-output read guard applies per SSE call, ignores heartbeats and Tool
execution, never a whole stage/Turn. Warn before switches, block uncertain
effect replay, and meter every physical call.

Tool-enabled HER Direct/Primary Execution may propose interim commentary, but
only typed Persona-packaged output is user-facing. Raw, packaging-failed, and
other-stage provider progress stay internal. Backend API may expose only the
typed ephemeral `answer_preview` after visibility checks; raw `text_delta` and
control stages stay private, final response authoritative. Suppress DeepSeek
AntML after commentary; repair through native `tool_calls`, never run it.

HER Agents manage Cron, Heartbeat, and Nudge through typed
`hashi_scheduler_create/update/delete` and Superloops through
`hashi_superloop_*`. Backend API binds reads/writes to the current Agent;
deletion needs explicit authorization. Never edit `tasks.json` or Superloop
files directly. These Functions need normal Worker/Function adoption checks.

Validate a Tool batch before effects. Malformed batches execute zero calls;
completed calls never replay. Repair preserves Provider fields, identity,
finish/error, and retry count. Continuation is not retry, prose “stop” is not a
typed stop, and degraded intent cannot complete a request without native repair.

Capture each request through terminal receipt, keeping restricted originals
separate from safe projections; partial evidence is not empty. `/stop`,
`/retry`, `/resend`, and `/steer` keep distinct contracts. Recovery never
duplicates a Cron Run, replays effects, restores revoked authority, or accepts
unknown effects. See [HER v2](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md).

## TUI, Workbench, and media

TUI renderers/catalogues own interface text. `/language` changes shared UI and
`/tui language` only local TUI; neither translates replies, IDs, commands,
paths, logs, or transcripts. Its highest scope is the selected instance:
switching binds generation, Agent, target, capabilities, logs, and sends, and
submission freezes them with the Session. Remote requires an authenticated
handshake; cached liveness grants nothing. Persist preferences only after
success.

`/telegram off` affects only that TUI Run. `/think` controls provider reasoning
and `/commentary` explicit Engine commentary. Media are committed Session
assets bound to one draft, instance, Agent, and Run; Remote sends managed bytes,
references stay inside enabled Workzones, and uncertain writes never switch
routes. HER carries authorised manifests as native content or exact managed
references without widening authority. External adapters use Session Runs for
text, media, Canvas, approvals, and voice. Safe Voice requires typed
`voice_message` confirm/discard; missing idempotency fails before upload. Late
or cancelled media is discarded, and optional STT stays in an isolated sidecar.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md); `/help` derives
from metadata. Workbench and Telegram share a Session while UI rows stay out of
model history. Durable command reservation returns saved completions, never
replays pending/unknown, and does not overstate `not_observed` as delivery;
non-Telegram callbacks keep their own Connector fence.

`/new` selects a fresh primary Session without deleting owner/Agent history;
old messages stay read-only and attachments keep their original Session. PAO
owns Agent deletion with preview, blockers, and cleanup receipts. Workbench
`/telegram` persists per owner; TUI preference is per Run.

## Move, Clone, jobs, and HCC

`/move` and `/clone` share package, registry, workspace, Scheduler, secret, and
lifecycle owners. Move removes verified source only after activation; Clone
preserves it, excludes Telegram credentials, and disables imported jobs.
`accepted` is not `completed`. See [Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler recurrence stores UTC instants, wall time, and IANA zone; legacy
unknown zones use UTC. Telegram recovery binds instance/lifecycle/Bot and
bounded retries honor `RetryAfter`.

Use authorized capabilities only; device actions need a same-instance Worker.
Prefer `log_query`; Agents work foreground unless `/bg` is explicit. Tests prove
scope, not live adoption; preserve user work and report failures.

HCC is optional, non-authoritative PCM context; `/hcc` and `hcc-refresh`
refresh sources; do not rewrite PCM or retry.

### FC terminal-only command boundary (2026-09-26)

`/logo` is TUI-only, hidden from shared command menus and disabled for every
non-TUI Connector, including new third-party registrations. The shared registry
projects this mandatory restriction into admission and capability discovery;
Connector overrides cannot enable it. The handler uses the normalized local
Connector identity so TUI execution remains available while native Telegram
gets a localized unsupported notice. Scope: HASHI1
`feature/fc-unified-io-20260925`; source/offline proof is recorded separately
from pending live adoption. See [Frontend Connector Architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#terminal-only-slash-commands)
and the three-frontend live test record.
