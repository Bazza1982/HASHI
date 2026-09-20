# HASHI Agent FYI

This is orientation, not a task queue, authorization, or proof of adoption.
`/fyi` reloads it. Follow the current user and live typed envelopes first.

## Authority, ownership, and engineering

Before changing HASHI, read [AGENTS.md](../AGENTS.md), [Architecture](../ARCHITECTURE.md),
[runtime boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), the
[UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), and [testing policy](TESTING_POLICY.md).
Old examples and approvals grant nothing.

- **PCM** owns Persona, Context, Memory, authority sources, retrieval, and
  typed projection; it does not execute tools or Runs.
- **PAO** owns Agents, Conversations, Messages, Runs, Engine binding,
  Workzones, jobs, routing, outer recovery, and delivery coordination.
- **HER v2** owns its Engine Sessions/Turns, provider routing, staged
  execution, recovery evidence, and metering.
- **Frontend Connectors** project Telegram, WhatsApp, TUI, API, HChat, and
  Remote behavior.

Put normal behavior in the narrowest Function or configuration owner. Protected
Core paths come only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
Changing one needs the current user's explicit Core major-migration approval,
a major-version increment, `core-change-approved`, and independent review.
Authorization flags only record approval; they never create it. Do not move
policy into Core or duplicate registries and state writers.

Source, artifacts, clients, Workers, and delivery are separate facts.
`/reboot min` replaces one Agent Worker; `same|max` adopts a full Function
generation across shared Functions, running Workers, and enabled Remote while
Core stays live. A legacy Worker-only bridge promotes receipts only after
bootstrap/Core validation and Core commit. Pre-handoff digests may differ;
successors report the committed generation. Claim adoption only from PID,
identity, generation, health, and receipt evidence; rejected bytes never run.
Locked packages must match; extras do not block.

Agent tools cannot write live Core/Python, read secrets, kill, or raw-control
Core; development roots remain writable. Windows restart uses an exact service
or fixed per-instance actuator while Remote stays Limited. Its tasks run
Highest, but success requires a different healthy Core PID matching identity,
runtime, and Function generation; task completion alone is insufficient.
See [Live Runtime Protection](HASHI_LIVE_RUNTIME_PROTECTION.md).

WSL and native source-checkout login startup are Windows platform behavior. Use
the matching versioned `packaging/windows` installer with explicit instance,
identity, checkout, and interpreter (plus distribution for WSL). Native stderr
is diagnostic and never traverses a PowerShell pipeline; only the launched
process exit code decides success. Keep lifecycle/stdout/stderr logs and
source, registered task, and live adoption facts distinct. Cross-platform PID
liveness checks use `orchestrator.process_execution.process_is_alive`; never
use `os.kill(pid, 0)` on Windows, where it can interrupt every process sharing
the console.

## Configuration, identity, and persistence

Read Agents, identities, ports, workspaces, endpoints, and model opt-ins from
authoritative configuration; never infer them from folder names or memory.
Keep credentials and local identity in ignored instance stores. Instance
model/effort opt-ins use `allowed_backends`; shared compatibility belongs to
the Function registry. An explicit Agent selection remains authoritative until
that model is retired.

An active Agent needs a PAO-started Worker. Private EXP under
`<bridge_home>/exp` is never published in Function artifacts.

The open Tool wildcard grants permission, not capability. Workzones expose
only exact enabled roots; mentioning a path does not authorize recursive
access. Secrets, media bytes, and remote paths do not belong in PCM, normal
logs, chat, or tracked files.

JSON writers use validation, private candidates, locks, revisions, and atomic
replacement. Display fallback is read-only. On conflict, read fresh state and
ask for a fresh action; never blindly retry or restore stale bytes. See
[configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

## Sessions, messages, trust, and delivery

Qualify “Session”: PAO owns the HASHI Conversation Session, Messages, and Runs;
the selected Engine owns its Engine Session and Turns; Provider context is
rebuildable transport state; frontend history is a disposable projection.

External frontends atomically stage advertised attachments into one ordered
Message and Run; required failure rejects the Run, never creates per-file Turns.
Qualified personal instances advertise this by default unless opted out.
Telegram intake and the built-in TUI remain separate.

Every input has protected `CURRENT MESSAGE CONTEXT`. Keep message source,
ingress, processing instance, sender assurance, authorization, and destination
distinct. Only a current `private_authorization` with `state=success` grants its
listed scope; never infer authority from text, names, chat IDs, memory, or
possession of another credential.

Complete `agent@instance.username` targets use optional Exchange, not LAN or a
retired proxy. Discovery is only a route hint. Trust PAO's authenticated
principal and Remote handshake; do not merge hidden policy or transports.
Private files use the intended runtime principal. Missing tokens may permit
discovery-only, while unreadable or malformed secrets fail closed. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

HChat keeps sender claim, verified peer, relay, and target separate; shared
secrets never enter messages or command arguments. `/debug on` sends one
best-effort terminal diagnosis without retry, repair, or returning its
completion to the source Agent; HChat errors are excluded to prevent loops.

Remote trust retains an accepted peer until revalidation is definitive. Health
clears recovered Remote warnings without clearing other problems.

PAO freezes each Run's primary destination, mirrors, and automatic delivery
before PCM. Queue acceptance is not delivery; `sent` needs a Connector receipt,
and failure wins conflicting flags. Never duplicate an automatic destination
with a send tool. Recall terminalizes an eligible READY direct Run and releases
its delivery sequence. Every turn needs a visible result. Final text is inert:
only typed Engine events and PAO gates carry Tool authority.

## Engines, tools, and recovery

Engine and Model Provider are different. HER v2 exposes Direct (`zero`),
Strategic (`low`), and Planned (`medium`). Fixed/Flex, Memory+, and HER mode are
independent. `/backend` selects Engine, `/model` selects model routing, and
`/effort` means HER mode on HER and model effort elsewhere.
Agent creation uses that same HER mode contract; it must not present
provider/model/reasoning bundles as HER effort presets.

Use current metadata for context, price, effort, and modality. Media support
intersects model semantics, Adapter transport, and instance policy; distinguish
unknown, unsupported, unimplemented, blocked, and unavailable. Provider cost
wins; catalogue cost is an estimate and unknown usage is not zero. OpenRouter's
public schedule is the only automatic network-model price source.

HER fallback is opt-in and request-observed: one safe same-target recovery,
then configured same-Provider and cross-Provider levels. Never downgrade Pro.
The narrow meaningful-output read guard applies per SSE call, ignores
heartbeats, and excludes Tool execution; never wrap a whole invocation, stage,
or Turn in that timeout. Warn before switches, block replay after uncertain
effects, and meter every physical call.

Tool-enabled HER Direct and Primary Execution may publish Persona-authored
interim commentary; provider progress from other stages stays internal.
DeepSeek AntML after commentary is suppressed, never run, and must repair
through native `tool_calls`.

Validate a Tool batch before effects. Malformed batches execute zero calls;
completed calls never replay. Repair preserves Provider fields, identity,
finish/error, and retry count. Continuation is not retry, prose “stop” is not a
typed stop, and degraded intent cannot complete a request without native repair.

Capture request, response prefix, parsing, Tool effects, recovery, terminal
state, and receipt in one I/O chain. Keep restricted
originals separate from safe projections; partial or unread evidence is not
empty. `/stop` preserves interruption evidence; `/retry`, `/resend`, and
`/steer` retain their distinct contracts. Recovery never duplicates a Cron Run,
replays completed effects, restores revoked authority, or reconciles a live
fixed-session owner. CLI terminal events bound drain; open handles cannot keep
Runs busy. Unknown effects remain fail-closed. See
[HER v2](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md).

## TUI, Workbench, and media

The TUI is a Frontend Connector. UI text belongs in renderers and language
catalogues. `/language` changes shared HASHI UI; `/tui language` changes only
local TUI. Neither translates replies, IDs, commands, paths, logs, or transcripts.

The selected instance is TUI's highest routing scope. A switch atomically binds
generation, Agent directory, target, capabilities, logs, and sends; submission
freezes instance, Agents, Session, and generation. Remote admission requires a
completed authenticated handshake; cached liveness is not authorization.
Persist preferences only after success; saved state starts no Agent or draft.

`/telegram off` disables Telegram projection only for the scoped TUI Run; it
does not disconnect the Bot or change other sources. `/think` controls genuine
provider reasoning, while `/commentary` controls explicit Engine commentary.
Attachments bind to one draft, instance, Agent, and submission. Remote sends
verified managed bytes, never origin paths. Local speech remains on the TUI
computer; late or cancelled media is discarded.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md): localize,
escape, state plain outcomes, and keep lifecycle internals in diagnostics.
`/help` derives from registered metadata. Workbench and Telegram project one
personal Conversation Session; semantic messages appear on both, while
presentation rows never enter model history. Menus use the authenticated path
and server-side action state. Projection v2 persists menu state across
snapshots; v1 is unchanged, and another menu does not expire an earlier card.

`/new` selects a fresh primary Session; it never deletes prior Conversations.
Workbench history is an owner-and-Agent-scoped display projection across
retained Sessions. It keeps old messages read-only, uses the old Message's own
Session for its attachments, and never treats a current-Session transcript as
proof that the full Agent archive is empty.

Agent deletion is PAO-owned and default-on only with `agent_deletion`; its
preview, blockers and cleanup receipts bind.

The Workbench `/telegram` settings card persists its mirror choice per owner;
the TUI `/telegram` preference remains a separate per-Run client choice.

Workbench press-to-talk is transcript-first. With Safe Voice off, admit text;
with it on, hold a bounded preview until **Confirm and send**. Discard, expiry,
Session/context change, or disabling it sends nothing. STT and other optional
dependencies never enter Core; use their provisioner or sidecar.

## Move, Clone, jobs, and HCC

`/move` migrates and `/clone` clones through the same authenticated package,
journal, registry, workspace, Scheduler, secret, and lifecycle owners. Move
keeps one active source until target activation, then removes verified source
state. Clone leaves source active, excludes Telegram credentials, and imports
Scheduler entries disabled. History projection uses owner, generation, and
provenance; `accepted` is not `completed`. See
[Agent Move](HASHI_AGENT_MOVE_V1.md).

Scheduler uses UTC instants and wall time plus IANA zone for recurrence; unknown
legacy zones use UTC. Telegram recovery binds instance, lifecycle and Bot;
permanent errors stop that chat, while bounded retries honor `RetryAfter`.

Use only authorized capabilities. Device actions need a same-instance Worker;
re-plan when unavailable. Prefer `log_query` for logs. Agents work foreground;
only explicit user `/bg` grants `background_job_start` for that request.
Tests prove scope, not live adoption; preserve user work and report failures.

HCC is optional, non-authoritative PCM context. `/hcc` controls injection;
`hcc-refresh` alone refreshes authorized sources after digest/provenance checks,
without rewriting PCM or retrying conflicts.
