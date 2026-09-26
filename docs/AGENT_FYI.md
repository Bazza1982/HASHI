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
Telegram reboot notice attempts belong in persistent `logs/bridge.log`, keyed
by the reboot receipt ID and start/final kind. Failures log safe codes and
retry delays; Bot credentials and raw transport exceptions stay out of logs.

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

HER v3 began on HASHI3 and was promoted as a harness-only series into HASHI1's
local `main` on 2026-09-26 beside the independent FC work. HASHI3, HASHI2,
HASHI4, and GitHub `main` remain outside that adoption. It stays inside the HER
functional owner: public Engine ID `her-v3`, internal `her-v2` adapter/storage
compatibility only, one main-model/tool loop, and optional JEV companion off by
default.

Engine and Model Provider differ. `/backend` selects Engine, `/provider`
Provider, `/model` model, and `/effort` Provider reasoning; Fixed/Flex and
Memory+ remain independent. Old v2 stages and `/herv2` are not public v3
controls; `/metre` aliases `/meter`. Model/provider cards and callbacks share
one contract, so test generated buttons, not only text commands. HASHI3 live
acceptance covered real switching, reasoning, Session identity, usage, and
metering; see [HER v3 experiment](HER_V3_EXPERIMENT.md) for evidence and the
separate HASHI1 promotion record.

Use current metadata for context, price, effort, and modality. Media needs
model, Adapter, and policy support; unknown is not unsupported or zero cost.
Fallback is opt-in and request-observed; warn before switching, block uncertain
effect replay, and meter every physical call.

Only typed Persona-packaged progress is user-facing; raw deltas and control
stages stay private, with final response authoritative. Scheduler and Superloop
writes use their typed tools, never direct state-file edits. Validate a Tool
batch before effects; malformed batches execute nothing and completed effects
never replay. Terminal receipts keep restricted originals separate from safe
projections. `/stop`, `/retry`, `/resend`, and `/steer` remain distinct;
recovery never restores revoked authority or accepts unknown effects.

## TUI, Workbench, and media

TUI renderers/catalogues own interface text. `/language` changes shared UI and
`/tui language` only local TUI; neither translates replies, IDs, commands,
paths, logs, or transcripts. Its highest scope is the selected instance:
switching binds generation, Agent, target, capabilities, logs, and sends, and
submission freezes them with the Session. Remote requires an authenticated
handshake; cached liveness grants nothing. Persist preferences only after
success.
Remote's optional `instances.json` compatibility view writes only when its
projected peer state changes; absent and empty optional fields compare equal.

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

`/logo` is TUI-only: hidden and denied on every other Connector, including
third parties, while normalized TUI admission remains available. Scope is
HASHI1 local `main`; source, offline proof, and live adoption remain separate.
See [Frontend Connector Architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#terminal-only-slash-commands).
