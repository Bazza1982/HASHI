# HASHI Agent FYI

- `/forget` removes traceable period data; untraceable summaries may remain.
  Audit copies stay outside Context. [PCM](HASHI_PCM_FORGET_2026-10-28.md).

- Task/local /sys deletion: authenticated management + current snapshot;
  exclude global/permanent prompts. [PAO](HASHI_PAO_SYSTEM_DESIGN.md),
  [PCM](HASHI_PCM_SYSTEM_DESIGN.md).

- Legacy audio migration requires verified ownership and safe paths; PAO
  settlement is cancellation-safe and HERV3 checkpoint claims are immutable.
  [audio](HASHI_NATIVE_AUDIO_CHAT_DESIGN.md#1841-legacy-native-output-reconciliation),
  [PAO](HASHI_PAO_SYSTEM_DESIGN.md), [HERV3](HERV3_UPGRADE.md).
- PAO strips internal control envelopes before judging deliverability. A result
  containing only Memory+ state fails terminally; typed audio remains deliverable.
- HERV3 has no artificial whole-run size/time/call ceiling. Preserve state and
  evidence; cancellation, safety, transport, Provider and single-Tool limits
  remain. [HERV3](HERV3_UPGRADE.md#2026-10-19-long-running-execution-invariant).
- Move: staging checks, forward-only confirmation, no rollback. Clone: PAO-owner
  snapshots, resumable retries, no Move-only gates/source lifecycle writes.
  [Move/Clone](HASHI_AGENT_MOVE_V1.md).
- Image fallback: selected model, same-Provider visual model, LAN Compute;
  visible/request-only. [admission](repairs/HERV3_VISUAL_FALLBACK_20261018.md).
- Compute uses PAO's fail-closed Function directory; no silent LAN-to-internet
  fallback. [Compute](HASHI_COMPUTE_SERVICE_DIRECTORY.md).
- Call frames are input-only/cleared camera-off; LAN targets are literal private
  IPv4 without redirects. Preview is not Agent vision. [Call](HASHI_CALL_INTERACTION_2026-10-04.md).
- Tool screenshots use provider-native image input, never Base64 text. Ollama
  behavior and public Provider identities follow [HERV3](HERV3_UPGRADE.md).

## Authority and ownership

PCM owns Persona/Context/Memory; PAO owns Agents/Sessions/Runs/jobs/Workzones and
delivery; HERV3 owns Engine Turns/model/tool loop/cost; Frontends authenticate and
render. Use the narrowest Function/config owner. Core has no product policy or
imports. Core changes require major-migration approval, version bump, label and
independent review; flags grant nothing.

Prove source, Worker adoption and delivery separately. Tools cannot edit/kill
Core/Python, read secrets or grant authority. `/reboot min|same` replaces one
Worker; `max` replaces shared Functions/Workers, not Core/Remote. Code adopts by
reboot, docs do not. Workbench preserves `AGENT_BUSY`; retry only before a durable
receipt; `target_busy` stays pending. `max` rejects shared API/background work
before and after fencing. [reboot](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md),
[fail-fast](HASHI_REBOOT_RECEIPTS.md#shared-handoff-fail-fast-correction-2026-10-22).

`hashi update` must adopt source, Core, Workers and Remote together or fail.

## Configuration, identity, and persistence

Isolated speech locks/runtimes/releases need separate proof:
[dependencies](DEPENDENCIES.md#security-maintenance-of-optional-runtime-locks).

Config owns identity, ports, Workzones, endpoints/models; exclude secrets and
machine paths. Opt-ins use `allowed_backends`/runtime effort; shared compatibility
uses the qualified registry. Unknown capability/price is neither unsupported nor
zero; explicit choices persist. HERV3 inherits [Provider policy](HASHI_MODEL_AVAILABILITY.md)
without Engine grants.

Agent name/Emoji use revisioned config; refresh cannot undo durable success.

Verified capabilities survive lookup failure until identity/source changes;
pricing freshness is separate. [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable has no credentials; PAO starts Workers. Wildcards grant permission, not
capability; paths grant nothing. Workzones are Agent-scoped; busy Runs freeze
active revisions while saved state stays visible. Windows rejects POSIX Workzones.
A Browser Bridge disconnect never stops the shared Windows named-pipe listener.
Keep experiments, secrets, media bytes and remote paths out of PCM/logs/chat/Git.

Agent stop is `stopped`; Worker outage `offline`; config deactivation `inactive`.
Shared Functions retain stop until restart; Frontends never rewrite `is_active`.

JSON writes validate private candidates under lock/revision, then atomically replace.
Display fallback is read-only; conflicts need fresh state/action, never blind
retry or stale restore. [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication retries only bounded Windows rename blocking on the same
fsynced candidate; permanent failure preserves state and never replays actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider/frontend
history is rebuildable. Preserve order; stage attachments atomically per Message/Run;
never leak failed/cancelled assets. Telegram item proofs keep partial uncertainty.
Diagnostics join Tool/job evidence.

Non-voice attachments commit atomically to the Agent's ignored
`.attachments/{session}/{message}/`; only its home can read them.
Session retention deletes them.

Telegram proactive routes are owner-only; actor/egress revalidate after restart.
Only the owner's same-ID private chat may bind. `/askme` controls instance and
Agent cards; off asks in plain text. Cards offer original-task continuation and
Agent stop. Question `unknown` neither cancels nor retries; Codex expiry preserves
authenticated partial answers. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md)
and [questions](HASHI_RUN_QUESTIONS.md).

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

Telegram: separate Bot-bound app/ingress readiness; Agent-only activation.
Bounded polls preserve pending updates. [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

## Engine, tools, and recovery

Retired Gemini CLI: use Antigravity; retain history/usage and qualified Gemini
models, not an account ban. [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

Public HERV3 is `her-v3`; `her-v2` is compatibility only. Telegram cards show
the public ID. JEV defaults off.
Backend/provider/model/effort, Fixed/Flex and Memory+ remain distinct. `/meter`
counts physical calls/cost; cumulative Codex usage needs a persisted baseline.

Privacy Level 2 is HERV3/DeepSeek-only, risk-accepted and fail-closed on
detector errors. Isolated dependencies or synthetic readiness prove neither
activation nor Worker adoption.

Chat-only has no tools. `/fallback`: `OFF` default; `RETRY` waits 60/180/300
or longer `Retry-After`; `ASK` fails closed; `AUTO` uses exact routes. A pauses
after effects; B resumes the original Engine Session, never replay/retarget.
PAO—not HERV3—switches to Codex; counts/costs accumulate. Phone/call/voice
never long-retry or fallback. See [policy](HASHI_MODEL_FALLBACK_POLICY.md).

## UI, media, and Phone

Manual Desktop stale input stays unexecuted until a fresh frame and never replays;
other failures release control. See [desktop](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

Local cascade is opt-in; synthetic proof is not physical acceptance. Live Phone
defaults on; missing key blocks and appears in `/phone`; opt-out remains valid;
settings/PCM do not prove provider readiness.

Renderers/catalogs own UI text. `/language` is shared, `/tui language` local;
neither translates replies/IDs. TUI switches freeze runtime identity/state.
Remote authenticates. Mirror opt-outs preserve origin replies; `/think` and
`/commentary` are independent.

`/stop` fences every Agent Session, cancels owned work, rejects stale callbacks,
retains periodic definitions, and durably records `stopped` before cleanup.
Never retry uncertain saves; HERV3 releases its Turn first. See
[stop contract](HASHI_AGENT_STOP_FENCE_DECISION.md).

Media bind draft/instance/Agent/Run; Remote grants no wider authority. Safe
Voice confirm/discard is typed/idempotent. Codex Session discard ends the Run
without a model call or error. Workbench retains `voice_message` audio but hides
its duplicate player/bubble; ordinary audio stays visible. Telegram uses its renderer.

Phone binds the owner's foreground Conversation. Use dialogue-round HCC without
raw metadata and bounded PCM/Conversation/job evidence, not scheduled prompts;
lookups create no Run. Action snapshots/constraints survive reconnection with
epoch-scoped authority; after checks, handoff freezes ID and ordered snapshot
without authority. Hang-up restores history. Bounded cleanup verifies event IDs,
not display text; exhaustion keeps context and allows the turn.
PCM stores arrival bounds; HERV3 orders Turns by time, skips unused history. Functions
own settings; open when ready.
Failed PCM refresh allows only stop/inspect; Session fence/hang-up win. Preserve
failure cause; resume at most five times, never retry uncertain writes. Verify
acceptance/execution/playback/listening and source/adoption/device separately.
See [PCM](HASHI_PCM_SYSTEM_DESIGN.md), [PAO](HASHI_PAO_SYSTEM_DESIGN.md),
[handoff](HASHI_PHONE_CONTEXT_HANDOFF.md) and [cascade](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Command history redacts args; originals execute. PAO notices keep Session/locale
without Workers; check scope, retry delivery only. Remote pages fairly; closed
cards keep text. [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).
`/new` preserves history; PAO owns Agent deletion.

`/say` reads the latest final; `2`–`4` choose a count and `1-3` the latest
three, oldest first. Skip cost/command/progress. Telegram needs a receipt;
Workbench/TUI play locally; reboot receipts never prove playback.

`/voice` and `/call` share privacy-checked routing before publish/cached
delivery. AP prefers VideoTTS unless `video_tts_supported: false`; without AP
it uses audio. Groups have bounded admission and ordered playback. Audio failure
holds video; fallback is group-local, stale tails cancel, late jobs only signal
recovery. Audio stays serial. See [language voices](HASHIKO_LANGUAGE_VOICES.md),
[Call/Phone policy](HASHI_CALL_AGENTPRESENCE_VIDEO.md#command-policy-2026-10-23),
[catalogue](HASHI_VOICE_TTS_CATALOG.md),
[routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md) and
[fallback](repairs/VOICE_PROVIDER_FALLBACK_REPAIR_20261014.md).

AgentPresence is owner-scoped/default-off; HASHI authenticates and freezes the
Agent/Conversation. Only PAO-admitted human/client input triggers owner-route
feedback; AP-local input never echoes. `/phone` sends scoped leave/return; AP
owns presentation, HASHI services/voices. Ingress fails closed; surfaces stay
independent. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md) and
[input feedback](HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md).

Deploys add Edge/Kokoro/SAPI without changing settings; the 957 MB portable keeps
Edge/SAPI/Piper, omits Kokoro/Torch. See
[deployment](repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

[Records](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
