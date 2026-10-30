# HASHI Agent FYI

- `/forget` removes traceable period data; untraceable summaries may remain.
  Audit copies stay outside Context. [PCM](HASHI_PCM_FORGET_2026-10-28.md).

- Task/local /sys deletion needs authenticated management and a current snapshot;
  exclude global/permanent prompts.

- Legacy audio needs ownership/path proof; PAO settlement is cancellation-safe
  and HERV3 checkpoint claims immutable. [audio](HASHI_NATIVE_AUDIO_CHAT_DESIGN.md).
- PAO strips internal control envelopes before deliverability checks.
  Memory+-only results fail terminally; typed audio remains deliverable.
- HERV3 has no whole-Run size/time/call ceiling. Keep state/evidence; cancellation,
  safety, transport, Provider and single-Tool limits remain. [HERV3](HERV3_UPGRADE.md).
- Agent Move checks mutable staging state; confirmed moves go forward only.
  Never roll the target back. [Move](HASHI_AGENT_MOVE_V1.md).
- Image fallback: selected model, same-Provider visual model, LAN Compute;
  visible/request-only. [admission](repairs/HERV3_VISUAL_FALLBACK_20261018.md).
- Compute uses PAO's fail-closed Function directory; no silent LAN-to-internet
  fallback. [Compute](HASHI_COMPUTE_SERVICE_DIRECTORY.md).
- Call frames are input-only/cleared camera-off; LAN targets are literal private
  IPv4 without redirects. Preview is not Agent vision. [Call](HASHI_CALL_INTERACTION_2026-10-04.md).
- Tool screenshots use provider-native image input, never Base64 text. Ollama
  behavior and public Provider identities follow [HERV3](HERV3_UPGRADE.md).

## Authority and ownership

PCM: Persona/Context/Memory. PAO: Agents/Sessions/Runs/jobs/Workzones/delivery.
HERV3: Engine Turns/model/tool loop/cost. Frontends: authentication/rendering.
Use the narrowest Function/config owner. Core has no product policy/imports;
changes need major-migration approval, version bump, label and independent review.
Flags grant nothing.

Prove source, Worker adoption and delivery separately. Tools cannot alter/kill
Core/Python, read secrets or grant authority. `/reboot min|same`: one Worker;
`max`: shared Functions/Workers, never Core/Remote. Code needs reboot; docs do not.
Preserve `AGENT_BUSY`; retry only before durable receipt; `target_busy` stays
pending. `max` rejects shared API/background work before/after fencing.
[reboot](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md),
[fail-fast](HASHI_REBOOT_RECEIPTS.md).

`hashi update` must adopt source, Core, Workers and Remote together or fail.

## Configuration, identity, and persistence

Isolated speech locks/runtimes/releases need separate proof:
[dependencies](DEPENDENCIES.md#security-maintenance-of-optional-runtime-locks).

Config owns identity/ports/Workzones/endpoints/models; omit secrets/machine paths.
Opt-ins: `allowed_backends`/runtime effort; shared compatibility: qualified registry.
Unknown capability/price means neither unsupported nor zero. Persist explicit
choices. HERV3 inherits [Provider policy](HASHI_MODEL_AVAILABILITY.md), not Engine grants.

Agent name/Emoji use revisioned config; refresh cannot undo durable success.

Verified capabilities survive lookup failure until identity/source changes;
pricing freshness is separate. [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable has no credentials; PAO starts Workers. Wildcards grant permission,
not capability; paths grant nothing. Agent Workzones freeze active revisions for
busy Runs; saved state stays visible. Windows rejects POSIX Workzones. Browser
disconnects preserve the shared Windows named-pipe listener. Keep experiments,
secrets, media bytes and remote paths out of PCM/logs/chat/Git.

Agent stop is `stopped`; Worker outage `offline`; config deactivation `inactive`.
Shared Functions retain stop until restart; Frontends never rewrite `is_active`.

JSON writes validate private candidates under lock/revision, then atomically replace.
Display fallback is read-only; conflicts need fresh state/action, never blind
retry or stale restore. [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication retries only bounded Windows rename blocking on the same
fsynced candidate; permanent failure preserves state and never replays actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Rebuild frontend
history from PAO; caches must match its current owner/Agent Session. Preserve
order, atomic attachments and failed/cancelled-asset isolation. Telegram proofs
retain partial uncertainty; join Tool/job diagnostics.

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

Gemini CLI is retired: reject it; use configured Antigravity and its model IDs.
Preserve history/usage and qualified Gemini models; old-client rejection is not
an account ban. See [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

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
raw metadata; bound PCM/Conversation/job evidence, never scheduled prompts.
Lookups create no Run. Reconnection retains action snapshots/constraints and
epoch-scoped authority. Checked handoffs freeze ID/ordered snapshot, not authority.
Hang-up restores history. Bounded cleanup checks event IDs, not display text;
exhaustion preserves context and permits the Turn. PCM bounds arrivals; HERV3
orders Turns by time and skips unused history. Functions own settings; open when
ready. Failed PCM refresh permits only stop/inspect; Session fence/hang-up win.
Keep failure causes; resume at most five times, never retry uncertain writes.
Prove acceptance/execution/playback/listening and source/adoption/device separately.
See [PCM](HASHI_PCM_SYSTEM_DESIGN.md), [PAO](HASHI_PAO_SYSTEM_DESIGN.md),
[handoff](HASHI_PHONE_CONTEXT_HANDOFF.md) and [cascade](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session
and remain owner-admitted during active Runs. `/new` preserves history; PAO owns
Agent deletion/cleanup receipts.

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

[Older checkpoints](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
