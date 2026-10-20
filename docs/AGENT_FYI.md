# HASHI Agent FYI

October 20 PAO Run finalization: classify deliverability after stripping
recognized internal control envelopes. A success containing only Memory Plus
state is terminally failed and completes visible request activity; raw non-empty
provider text can no longer leave a Run stuck in `running`. Typed audio remains
deliverable. Approved and source-validated on HASHI3 `debug-issues-oct`;
Function adoption and live acceptance are recorded separately.

October 19 HERV3 long work: foreground execution has no HASHI total token,
message, Turn, model-call, Tool-call, elapsed-time, output, continuation,
receipt, WIP or task-state ceiling. Privacy Level 2 changes inspection, not loop
size. Continuation/state/evidence are lossless; large privacy inspection and
durable Provider context are paged/segmented rather than truncated. Retain only
explicit cancellation, progress-idle and transport/protocol guards, explicit
single-Tool timeout, external Provider limits and replay safety. Source/offline
verification do not prove Worker adoption or live acceptance. See
[repair](repairs/HERV3_LONG_RUNNING_LIMIT_REMOVAL_20261019.md) and
[HERV3](HERV3_UPGRADE.md#2026-10-19-long-running-execution-invariant).

October 19 Agent Move: staging is the single mutable-state qualification gate.
Confirmation records forward-only intent; later Memory+, workspace, config,
credential or schedule changes cannot invalidate the accepted package. Runtime
and cleanup failures retry forward without target rollback or source restore;
cancel is available only before confirmation. See
[Agent Move](HASHI_AGENT_MOVE_V1.md#forward-only-move-decision-2026-10-19).

October 18 visual fallback: image requests first use the selected model, then an
allowed visual model from the same Model Provider, then LAN HASHI Compute Vision.
The switch is request-only and warned once; it never changes the Agent default or
silently crosses to another cloud Provider. On the LAN route, raw images are
removed before the selected cloud model receives derived text. Telegram exposed
an older connector preflight that saw only the selected text model and stopped
before HERV3 fallback. The shared HERV3 media diagnostic now derives the same
same-Provider model order and LAN availability as invocation, so every FC can
admit the same canonical image request. This correction is source-validated on
HASHI3 `debug-issues-oct` but not yet hot-loaded. Earlier real Workbench
Pro-to-Flash and LAN projector checks remain valid; full
all-text-Provider-to-LAN frontend acceptance remains pending. See
[decision](repairs/HERV3_VISUAL_FALLBACK_20261018.md).

October 18: Ollama native context, binary reasoning and main promotion:
[status/limits](HERV3_OLLAMA_CONTEXT_PILOT.md). Public Provider identities now
match live metadata; legacy IDs remain accepted. See [HERV3](HERV3_UPGRADE.md).

October 17 Compute: local/LAN/internet model services resolve through PAO's
Function-layer Compute Service Directory. Register from a v1 manifest and persist
structured node/service/model references. Capability, privacy, health,
concurrency, busy and interface checks fail closed; no silent LAN-to-internet
fallback. Hashi Compute stays truthful and limits unknown until advertised.
Voice samples use the remote interface; consumers migrate separately and no
Workbench page was added. See [Compute](HASHI_COMPUTE_SERVICE_DIRECTORY.md).

October 17 Call experiment: capture targets 2 fps; MiniCPM-V uses latest-frame
narration. Each turn freezes 10 seconds and samples at most 10 raw frames for a
qualified primary vision model. Frames are input-only, never PAO-persisted, and
cleared camera-off. See [Call](HASHI_CALL_INTERACTION_2026-10-04.md) and
[experiment](repairs/CALL_LIVE_FRAME_BRIDGE_EXPERIMENT_20261017.md).

October 16: HERV3/DeepSeek Tool screenshots become tool text plus user
`image_url`, not Base64 text. Enforce serialized provider limits, not the
smaller `media_read` budget. See [repair](repairs/DEEPSEEK_TOOL_SCREENSHOT_MULTIMODAL_20261016.md).

October 16: `/call` LAN targets allow only literal RFC 1918 IPv4; block DNS,
loopback, link-local/public addresses and redirects. Selection is per owner/
Agent. See [Call](HASHI_CALL_INTERACTION_2026-10-04.md).

October 13 Call: prepare camera during ringback; without a first frame use voice.
Preview is not Agent vision. Only stopped ingress or confirmed initial failure
disables it; comments stay rare/relevant. See
[Call](HASHI_CALL_INTERACTION_2026-10-04.md).

## Authority and ownership

PCM owns Persona/Context/Memory; PAO owns Agents/Sessions/Runs/jobs/Workzones
and delivery; HERV3 owns Engine Turns/model/tool loop/cost; Frontends
authenticate/render. Use the narrowest Function/config owner. Core has no product
policy/imports. Core changes need explicit major-migration approval, version
bump, label and independent review; flags grant nothing.

Source, Worker adoption and delivery need separate proof. Tools cannot edit/kill
Core/Python, read secrets or grant authority. `/reboot min|same` replaces one
Worker; `max` replaces shared Functions/Workers, never Core/Remote. Function code
adopts only by reboot; docs do not. Workbench config preserves `AGENT_BUSY`;
retry only before a durable receipt; durable `target_busy` stays pending. See
[reboot decision](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md).

## Configuration, identity, and persistence

Configuration owns identity, ports, Workzones, endpoints and models; exclude
secrets/machine paths. Model opt-ins use `allowed_backends`/runtime effort;
shared compatibility uses the qualified registry. Unknown capability/price is
neither unsupported nor zero; explicit choices persist.

Agent name/Emoji come from revisioned config; runtime supplies status. Failed
post-save Worker refresh cannot reverse durable success.

Verified exact-model capabilities do not expire or vanish on lookup failure;
changed model/adapter identity or validated source may replace them. Pricing
freshness is separate. See [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable carries no credentials; PAO starts Workers. Wildcards grant permission,
not capability; path names grant nothing. Runs freeze Workzone roots/revision
and reload only idle. Keep experiments, secrets, media bytes and remote paths
out of PCM, logs, chat and Git.

Agent stop is `stopped`; Worker outage `offline`; config deactivation `inactive`.
Shared Functions retain stop until restart; Frontends never rewrite `is_active`.

JSON writers validate private candidates under lock/revision and atomically
replace. Display fallback is read-only. On conflict, read fresh state/request
fresh action; never retry blindly or restore stale bytes. See
[configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication may retry only a bounded Windows sharing-blocked rename of
the same fsynced candidate. Permanent failure keeps prior durable state; never
advertise unwritten routes or replay actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider and
frontend history are rebuildable. Preserve order; stage attachments atomically
per Message/Run and never leak failed/cancelled assets.

Incremental deliverables bind exact files/stable publication IDs. Persistence,
receipts and UI visibility differ; uncertain delivery never auto-retries and
final attachments stay final-only. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables).

CURRENT MESSAGE CONTEXT separates source, assurance, authority and destination.
Only current successful private_authorization grants scope; names, text, IDs,
memory and credentials do not. HChat separates claimed sender/verified peer/
relay/target; cross-instance needs authenticated Remote. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations before PCM and owns ingress/routing/idempotency; FC owns
presentation/receipts. Remote attachment commits retain canonical HChat
idempotency and original send time; HChat IDs bind FC Sessions to Superloop
review. Sent needs a receipt; failure wins. Deduplicate/settle; prose grants no
Tool authority. Continuations retain Session/Connector and replay only saved
non-action completions; pending/conflict/unknown cannot execute.

Network frontends preserve original send time. PAO admits Workbench/Telegram
within 30/60 seconds; older/invalid messages need one durable 15-minute
confirmation before a Run. Expiry/discard stays visible. TUI exemption needs
Remote-signed local evidence; `/hchat` needs 60 seconds plus exact reverse-route
correlation. Qualified only on HASHI3/Workbench; production adoption is separate.

Scheduler/recovery and `/bg` use an Agent-owned hidden activity Session. Separate
execution/delivery/receipts; show each final once in its owning Conversation and
exclude Engine/Phone copies. `/bg` freezes admission; schedules get no implicit
chat; `/delay` and interactive `/loop` retain context.

HASHI3 Telegram is healthy only after a bounded poll; normal startup preserves
pending updates. HERV3 retains and may quote complete WIP, never premature recovery cards.
Source changes need separate Worker adoption/live checks.

## Engine, tools, and recovery

Gemini CLI is retired: reject it; use configured Antigravity and its model IDs.
Preserve history/usage and qualified Gemini models; old-client rejection is not
an account ban. See [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

Public HERV3 is `her-v3`; `her-v2` is compatibility only. JEV defaults off.
`/backend`, `/provider`, `/model`, `/effort` select named layers; Fixed/Flex and
Memory+ are independent. `/meter` counts physical calls/cost. Codex cumulative
usage needs a persisted baseline; otherwise estimate. USD is not a subscription bill.

Privacy Level 2 is HERV3/DeepSeek only, needs risk acceptance and blocks on
local-detector failure. Its dependencies use a separate interpreter; install or
synthetic readiness does not activate privacy or prove Worker adoption.

Model/provider cards share a contract; chat-only has no tools. `/style`
separates facts/meters. Fallback is opt-in; never replay uncertain/committed
effects. Reject bad Tool batches. Native Codex `/steer` keeps its Run; HERV3 links
one successor to a verified same-Session checkpoint. Keep command frontend
primary and mirrors secondary. Receipts omit reasoning; recovery grants no authority.

## UI, media, and Phone

Manual Desktop stale input stays unexecuted: keep its lease, await a fresh frame
and never replay; other failures release control. See [desktop](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

Local cascade speech is an opt-in Function sidecar; synthetic evidence is not
physical audio acceptance. See [local Phone](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Live Phone defaults on; missing provider key blocks and appears in `/phone`.
Instance opt-out remains valid. Phone settings/PCM readiness do not prove provider readiness.

Phone failures preserve causes: policy, auth, quota, rate limit, configuration,
provider outage. Failed/interrupted panels persist until dismissal; normal
hang-up may dismiss. Terminal calls settle attempts and busy state across replacement.

Renderers/catalogs own UI text. `/language` is shared, `/tui language` local;
neither translates replies/IDs. TUI switches freeze runtime identity/state.
Remote authenticates. Mirror opt-outs preserve origin replies; `/think` and
`/commentary` are independent.

`/stop` fences every Agent Session: cancel/reap owned work, clear pre-fence
one-shots, reject stale callbacks, retain periodic definitions. Save `stopped`
and terminal event before cleanup; repair adoption remnants. Never retry
unconfirmed saves; close DB after commit/rollback. HERV3 releases its Turn
before outer cleanup. See [stop contract](HASHI_AGENT_STOP_FENCE_DECISION.md).

Media bind one draft/instance/Agent/Run; Remote sends managed bytes and grants
HERV3 no wider authority. Safe Voice requires typed confirm/discard and
idempotency; discard stale media. `/voice` uses validated Function/local media;
Workbench gets Session audio, Telegram its renderer. Workbench keeps
`voice_message` audio durable but hides its generic attachment/player and any
transport-only bubble; ordinary audio remains visible. Its Safe Voice
confirmation card sizes to content.

Phone binds to the owner's foreground Conversation until hang-up. Actions get
bounded original PCM/Conversation/job evidence, excluding scheduled prompts;
lookups create no Run and required context must fit. See
[PCM](HASHI_PCM_SYSTEM_DESIGN.md) and [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Phone fragments are ordered durable Session speech. Prove acceptance, execution,
playback and listening separately; validate origin/order, deliver finals, never
auto-retry uncertain writes.

Phone handoff keeps its frozen ID only after scoped snapshot/delegation checks.
Pre-model errors settle Run/activity through the result owner; caught errors are
not success. Byte grouping preserves roles, text, event IDs, order/timestamps
and accepts old snapshots; it grants no authority/retry. See
[handoff](HASHI_PHONE_CONTEXT_HANDOFF.md).

Function adapters own Phone settings: apply next call and retain provider on
recovery. Open once with Persona/language after readiness; yield to speech.
Window movement cannot hang up. Failed PCM refresh permits stop/inspection only.
Sideband follows the Session fence; explicit hang-up wins stale faults. Keep
source, adoption and device acceptance separate.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session
and remain owner-admitted during active Runs. `/new` preserves history; PAO owns
Agent deletion/cleanup receipts.

`/say` reads the latest final; `2`–`4` choose a count and `1-3` the latest
three, oldest first. Skip cost/command/progress. Telegram needs a receipt;
Workbench/TUI play locally; reboot receipts never prove playback.

TTS logs omit secrets. `/voice` selects labelled voice/preview; >=90%
unsupported text switches within provider (English exempt). Workbench uses its
display locale, others `/language`. Visible fallback is paid/network to
free/local only; Windows Edge may use named SAPI. `/call` is unchanged. See
[routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md) and
[fallback](repairs/VOICE_PROVIDER_FALLBACK_REPAIR_20261014.md).

Deploys add Edge/Kokoro/SAPI without changing settings; the 957 MB portable keeps
Edge/SAPI/Piper, omits Kokoro/Torch. See
[deployment](repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

See [Call](HASHI_CALL_INTERACTION_2026-10-04.md); device acceptance is separate.
[Older checkpoints](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
