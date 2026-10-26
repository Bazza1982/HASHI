# HASHI Agent FYI

Current decisions, with full rationale in the linked owners:

- Legacy audio migration is fail-closed on verified ownership and safe paths;
  PAO settlement is cancellation-safe and HERV3 checkpoint claims are immutable.
  See [audio](HASHI_NATIVE_AUDIO_CHAT_DESIGN.md#1841-legacy-native-output-reconciliation),
  [PAO](HASHI_PAO_SYSTEM_DESIGN.md) and [HERV3](HERV3_UPGRADE.md#completed-tool-checkpoint-provider-call-continuation).
- PAO strips internal control envelopes before judging deliverability. A result
  containing only Memory+ state fails terminally; typed audio remains deliverable.
- HERV3 has no artificial whole-run size/time/call ceiling. Preserve complete
  state and evidence; only explicit cancellation, safety, transport, Provider and
  single-Tool limits remain. See [HERV3](HERV3_UPGRADE.md#2026-10-19-long-running-execution-invariant).
- Agent Move qualifies mutable state at staging, becomes forward-only after
  confirmation, and never rolls the target back. See [Move](HASHI_AGENT_MOVE_V1.md#forward-only-move-decision-2026-10-19).
- Image fallback is selected model, same-Provider visual model, then LAN Compute;
  it is visible/request-only and shares these [admission facts](repairs/HERV3_VISUAL_FALLBACK_20261018.md).
- Compute services resolve through PAO's fail-closed Function directory; no
  silent LAN-to-internet fallback. See [Compute](HASHI_COMPUTE_SERVICE_DIRECTORY.md).
- Call frames are input-only/cleared camera-off; LAN targets are literal private
  IPv4 without redirects. Preview is not Agent vision. See [Call](HASHI_CALL_INTERACTION_2026-10-04.md).
- Tool screenshots use provider-native image input, never Base64 text. Ollama
  behavior and public Provider identities follow [HERV3](HERV3_UPGRADE.md).

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
retry only before a durable receipt; durable `target_busy` stays pending. `max`
rejects shared API/background work before and after fencing. See [reboot decision](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md)
and [fail-fast](HASHI_REBOOT_RECEIPTS.md#shared-handoff-fail-fast-correction-2026-10-22).

`hashi update` must adopt source, Core, Workers and Remote together or fail.

## Configuration, identity, and persistence

Configuration owns identity, ports, Workzones, endpoints and models; exclude
secrets/machine paths. Model opt-ins use `allowed_backends`/runtime effort;
shared compatibility uses the qualified registry. Unknown capability/price is
neither unsupported nor zero; explicit choices persist.
HERV3 inherits [Provider policy](HASHI_MODEL_AVAILABILITY.md) without Engine grants.

Agent name/Emoji come from revisioned config; failed post-save refresh cannot reverse durable success.

Verified model capabilities survive lookup failure until identity/source changes;
pricing freshness is separate. See [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable carries no credentials; PAO starts Workers. Wildcards grant permission,
not capability; path names grant nothing. Workzones stay Agent-scoped across
backends; busy Runs freeze their active revision while saved state remains visible.
A Browser Bridge disconnect never stops the shared Windows named-pipe listener.
Keep experiments, secrets, media bytes and remote paths out of PCM/logs/chat/Git.

Agent stop is `stopped`; Worker outage `offline`; config deactivation `inactive`.
Shared Functions retain stop until restart; Frontends never rewrite `is_active`.

JSON writers validate private candidates under lock/revision and atomically replace.
Display fallback is read-only; conflicts require fresh state/action, never blind
retry or stale restore. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Local API credential publication and Remote-principal ACL repair run once in
shared Function bootstrap. Ordinary config reads and Agent Workers are
read-only; never move this maintenance back into `ConfigManager.load()` by
default or multiply it across `/reboot max`.

Endpoint publication may retry only bounded Windows rename blocking of the same
fsynced candidate; permanent failure keeps prior state and never replays actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider and
frontend history are rebuildable. Preserve order; stage attachments atomically
per Message/Run and never leak failed/cancelled assets.

Accepted non-voice attachments commit atomically to the target Agent's ignored
`.attachments/{session}/{message}/`; only its home scope may read them and
Session retention removes them. Safe Voice has its own lifecycle.

Telegram proactive routes are owner-only and revalidate actor/egress after restart.
Question `unknown` neither cancels nor retries; Codex expiry preserves authenticated
partial answers. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md) and
[questions](HASHI_RUN_QUESTIONS.md).

CURRENT MESSAGE CONTEXT separates source, assurance, authority and destination;
only current successful private_authorization grants scope. HChat separates
claimed sender/verified peer/relay/target; cross-instance needs authenticated Remote. See
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations before PCM and owns ingress/routing/idempotency; FC owns
presentation/receipts. Sent needs a receipt and failure wins. Prose grants no
authority; only saved non-action completions may replay.

Network frontends preserve send time. PAO admits Workbench/Telegram within 30/60
seconds; stale input needs durable confirmation. TUI exemptions need signed local
evidence; `/hchat` needs reverse-route correlation. HASHI3 is not production.

Scheduler/recovery and `/bg` use an Agent-owned hidden Session; separate
execution/delivery/receipts and show each final once. `/bg` freezes admission;
schedules get no implicit chat; `/delay` and interactive `/loop` retain context.

HASHI3 Telegram health needs a bounded poll; startup preserves pending updates.
HERV3 keeps complete WIP; source, Worker adoption and live proof stay separate.

## Engine, tools, and recovery

Gemini CLI is retired: reject it; use configured Antigravity and its model IDs.
Preserve history/usage and qualified Gemini models; old-client rejection is not
an account ban. See [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

Public HERV3 is `her-v3`; `her-v2` is compatibility only. Telegram cards show
the public ID. JEV defaults off.
Backend/provider/model/effort, Fixed/Flex and Memory+ remain distinct. `/meter`
counts physical calls/cost; cumulative Codex usage needs a persisted baseline.

Privacy Level 2 is HERV3/DeepSeek only, needs risk acceptance and blocks on
local-detector failure. Its dependencies use a separate interpreter; install or
synthetic readiness does not activate privacy or prove Worker adoption.

Model/provider cards share a contract; chat-only has no tools. Fallback is opt-in
and never replays uncertain effects. Codex `/steer` keeps its Run; HERV3 links one
verified successor. Command frontend stays primary; receipts omit reasoning and
recovery grants no authority. HERV3 resumes only its exact running PAO child.

## UI, media, and Phone

Manual Desktop stale input stays unexecuted until a fresh frame and never replays;
other failures release control. See [desktop](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

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

`/stop` fences every Agent Session, cancels owned work, rejects stale callbacks,
retains periodic definitions, and durably records `stopped` before cleanup.
Never retry uncertain saves; HERV3 releases its Turn first. See
[stop contract](HASHI_AGENT_STOP_FENCE_DECISION.md).

Media bind one draft/instance/Agent/Run; Remote grants no wider authority. Safe
Voice uses typed, idempotent confirm/discard. Workbench keeps `voice_message`
audio durable while hiding its duplicate generic player/bubble; ordinary audio
stays visible. Telegram uses its renderer.

Phone binds to the owner's foreground Conversation until hang-up. Actions get
bounded original PCM/Conversation/job evidence, never scheduled prompts; lookups
create no Run. See [PCM](HASHI_PCM_SYSTEM_DESIGN.md) and
[PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Phone speech is ordered and durable. Prove acceptance, execution, playback and
listening separately; uncertain writes never auto-retry.

Phone handoff freezes its ID only after scoped checks, preserves the full ordered
snapshot and grants no authority. See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md).

Function adapters own next-call settings and recovery. Open once after readiness;
window movement cannot hang up. Failed PCM refresh permits stop/inspection only;
sideband follows the Session fence and explicit hang-up wins stale faults. Keep
source, adoption and device acceptance separate.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session
and remain owner-admitted during active Runs. `/new` preserves history; PAO owns
Agent deletion/cleanup receipts.

`/say` reads the latest final; `2`–`4` choose a count and `1-3` the latest
three, oldest first. Skip cost/command/progress. Telegram needs a receipt;
Workbench/TUI play locally; reboot receipts never prove playback.

`/voice` and `/call` share routing; actual video/audio routes are
privacy-checked before publish or cached delivery. AP prefers VideoTTS
unless `video_tts_supported: false`; without AP it uses audio. Groups have
bounded admission and ordered playback. Audio failure keeps video waiting;
fallback is group-local; stale tails cancel; late jobs only signal recovery.
Audio stays serial.
See [language voices](HASHIKO_LANGUAGE_VOICES.md),
[Call/Phone policy](HASHI_CALL_AGENTPRESENCE_VIDEO.md#command-policy-2026-10-23),
[catalogue](HASHI_VOICE_TTS_CATALOG.md),
[routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md) and
[fallback](repairs/VOICE_PROVIDER_FALLBACK_REPAIR_20261014.md).

AgentPresence is owner-scoped and default-off. HASHI authenticates binding and
freezes the resolved Agent/Conversation. Only PAO-admitted human/client input
triggers owner-route feedback; AP-local input never echoes. `/phone` sends scoped
leave/return; AP owns presentation, HASHI owns services/voices. Ingress fails
closed; surfaces stay independent. See
[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md) and
[input feedback](HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md).

Deploys add Edge/Kokoro/SAPI without changing settings; the 957 MB portable keeps
Edge/SAPI/Piper, omits Kokoro/Torch. See
[deployment](repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

See [Call](HASHI_CALL_INTERACTION_2026-10-04.md); device acceptance is separate.
[Older checkpoints](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
