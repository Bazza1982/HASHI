# HASHI Agent FYI

- Fingerprint defaults OFF and freezes policy per Run. Evidence failure never
  replays committed writes; repair evidence only. Keep gaps/unsigned exports
  explicit. [PAO](HASHI_FINGERPRINT.md).

- Telegram/Remote：[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md)；Agent.md异步/HCC 409：[PCM](HASHI_PCM_SYSTEM_DESIGN.md)。

- 权限与验证：L1 模型超时通过 Adapter 配置传递，禁止赋值只读视图；
  续跑日志按执行 epoch 区分，保留原操作和不可变事实检查；
  完整 L1 前端实测仍独立验收。[状态](permissions/IMPLEMENTATION_STATUS.md)。

- `/forget` removes traceable period data; untraceable summaries may remain.
  Audit stays outside Context. [PCM](HASHI_PCM_FORGET_2026-10-28.md).

- Task/local /sys + parked deletion: authenticated, revision-checked;
  /sys excludes global/permanent, parked needs guarded Worker, keeps chat.
  [PAO](HASHI_PAO_SYSTEM_DESIGN.md), [PCM](HASHI_PCM_SYSTEM_DESIGN.md).

- Legacy audio needs verified ownership/safe paths; PAO settlement is
  cancellation-safe; HERV3 checkpoint claims are immutable. [audio](HASHI_NATIVE_AUDIO_CHAT_DESIGN.md#1841-legacy-native-output-reconciliation),
  [PAO](HASHI_PAO_SYSTEM_DESIGN.md), [HERV3](HERV3_UPGRADE.md).
- PAO strips internal envelopes before delivery; Memory+-only results fail
  terminally, while typed audio remains deliverable.
- HERV3 has no whole-run ceiling; preserve state/evidence. Safety, transport,
  Provider and single-Tool limits remain. [HERV3](HERV3_UPGRADE.md#2026-10-19-long-running-execution-invariant).
- Move stages and confirms forward-only, without rollback. Clone uses PAO
  snapshots/resumable retry, never Move gates/source writes. [Move/Clone](HASHI_AGENT_MOVE_V1.md).
- Image fallback uses the selected model, same-Provider vision or LAN Compute,
  and is visible/request-only. [admission](repairs/HERV3_VISUAL_FALLBACK_20261018.md).
  Compute uses PAO's fail-closed directory; no LAN-to-internet fallback.
  [Compute](HASHI_COMPUTE_SERVICE_DIRECTORY.md).
- Call frames are input-only/cleared camera-off; LAN targets are literal private
  IPv4 without redirects; preview is not vision. [Call](HASHI_CALL_INTERACTION_2026-10-04.md).
  Tool screenshots use native images, not Base64; Ollama/public Provider IDs
  follow [HERV3](HERV3_UPGRADE.md).

## Authority and ownership

PCM owns Persona/Context/Memory; PAO owns Agents/Sessions/Runs/jobs/Workzones/
delivery; HERV3 owns Turns/model/tool/cost; Frontends own authentication/rendering.
Use the narrowest Function/config owner. Core has no product policy/imports and
requires major-migration approval, version bump, label and independent review.
Flags grant nothing.

Prove source, Worker adoption and delivery separately. Tools cannot alter
Core/Python, read secrets or grant authority. `/reboot min|same`: one Worker;
`max`: shared Functions/Workers, never Core/Remote. Code needs reboot; docs do not.
Preserve `AGENT_BUSY`; retry only before durable receipt; `target_busy` stays
pending. `max` rejects shared API/background work around fencing.
[reboot](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md),
[fail-fast](HASHI_REBOOT_RECEIPTS.md).

`hashi update` adopts source, Core, Workers and Remote together or fails.

## Configuration, identity, and persistence

Prove isolated speech locks/runtimes/releases separately:
[dependencies](DEPENDENCIES.md#security-maintenance-of-optional-runtime-locks).

Config owns identity/ports/Workzones/endpoints/models; omit secrets/paths.
Instance opt-ins use `allowed_backends`/runtime effort; shared compatibility
uses the qualified registry. Unknown capability/price is neither unsupported
nor zero. Persist choices; HERV3 inherits [Provider policy](HASHI_MODEL_AVAILABILITY.md), not Engine grants.

Agent name/Emoji use revisioned config; refresh cannot undo a durable save.

Verified capabilities survive lookup failure until identity/source changes;
pricing freshness stays separate. [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable has no credentials; PAO starts Workers. Wildcards grant permission,
not capability; paths grant nothing. Frontend Workzone edits are Agent-scoped
and revisioned; Runs freeze snapshots and saved state stays visible. Windows
rejects POSIX Workzones; browser disconnects keep the shared named-pipe listener.
Keep experiments, secrets, media and remote paths out of PCM/logs/chat/Git.

Agent stop is `stopped`; Worker outage `offline`; config deactivation `inactive`.
Shared Functions retain stop until restart; Frontends never rewrite `is_active`.

JSON writes validate private candidates under lock/revision, then atomically
replace. Display fallback is read-only; conflicts need fresh state/action,
never blind retry/stale restore. [persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication retries only bounded Windows rename blocking on the same
fsynced candidate; permanent failure preserves state and never replays.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Rebuild
frontend history from PAO; caches match its current owner/Agent Session.
Preserve order, atomic attachments and failed/cancelled isolation. Telegram
proof remains partly uncertain; join Tool/job diagnostics.

Non-voice attachments commit atomically to the Agent's ignored
`.attachments/{session}/{message}/`; only its home reads them, and Session
retention deletes them. Access failures differ from integrity mismatches;
diagnostics omit paths.

Telegram proactive routes are owner-only; revalidate actor/egress after restart.
Only the owner's same-ID private chat binds. `/askme`: instance/Agent cards,
off: plain text. Offer original-task continuation/Agent stop. `unknown` never
cancels/retries; Codex expiry keeps authenticated partial answers.
[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md), [questions](HASHI_RUN_QUESTIONS.md).

CURRENT MESSAGE CONTEXT separates source/assurance/authority/destination. Only
successful current private_authorization grants scope. HChat separates claimed
sender/verified peer/relay/target; cross-instance requires authenticated
[Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations before PCM and owns ingress/routing/idempotency. FC owns
presentation/receipts. Sent needs a receipt; failure wins. Prose grants nothing;
replay only saved non-action completions.

Keep frontend send times. PAO admits Workbench/Telegram within 30/60 seconds;
stale input needs durable confirmation. TUI exemptions need signed local proof,
`/hchat` reverse-route correlation. HASHI3 is not production.

Scheduler/recovery/`/bg` use an Agent-owned hidden Session; execution, delivery
and receipts each finalize once. `/bg` freezes admission; schedules imply no
chat. `/delay` and interactive `/loop` retain context.

Telegram local/global gates differ; legacy grants remain. [PAO](HASHI_PAO_SYSTEM_DESIGN.md).
Telegram readiness requires separate Bot-bound app/ingress and a user;
activation is Agent-only. Bounded polls preserve pending updates.
[FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).

## Engine, tools, and recovery

Retired Gemini CLI: use Antigravity; retain history/usage and qualified Gemini
models; no account ban. [retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

HERV3: `her-v3`; `her-v2` is compatibility only. Telegram cards show
the public ID. JEV defaults off.
Backend/provider/model/effort, Fixed/Flex and Memory+ remain distinct. `/meter`
counts physical calls/cost; cumulative Codex usage needs a persisted baseline.

Privacy Level 2 is HERV3/DeepSeek-only, risk-accepted and fail-closed on
detector errors. Isolated dependencies or synthetic readiness prove neither
activation nor Worker adoption.

Chat-only has no tools. `/fallback` defaults `OFF`: `RETRY` waits 60/180/300
or longer `Retry-After`, `ASK` fails closed, `AUTO` uses exact routes. A pauses
after effects; B resumes the original Engine Session, never replay/retarget.
PAO—not HERV3—switches to Codex and accumulates counts/costs. Phone/call/voice
never long-retry or fallback. [policy](HASHI_MODEL_FALLBACK_POLICY.md).

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
Voice confirm/discard is typed/idempotent; Codex discard ends the Run without a
model call/error. Workbench retains `voice_message` but hides duplicate UI;
ordinary audio stays visible. Telegram uses its renderer.

Phone binds the owner's foreground Conversation. Dialogue-round HCC uses bound
PCM/Conversation/job evidence, never raw metadata/scheduled prompts; lookups
create no Run. Reconnect keeps snapshots/constraints/epoch authority; handoffs
freeze ID/order only; hang-up restores history. Cleanup matches event IDs;
exhaustion preserves context and permits the Turn. PCM bounds
arrivals; HERV3 time-orders and skips unused history. Functions own settings.
Failed PCM refresh allows stop/inspect only; fence/hang-up win. Keep causes;
resume at most five times, never retry uncertain writes. Prove acceptance,
execution, playback, listening, source, adoption and device separately.
See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md) and
[cascade](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Command history redacts args; originals execute. PAO notices keep Session/locale
without Workers; scope-check and retry delivery only. Remote pages fairly;
closed cards keep text. [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).
`/new` preserves history; PAO owns Agent deletion.

`/say` reads the latest final; `2`–`4` choose a count and `1-3` the latest
three, oldest first. Skip cost/command/progress. Telegram needs a receipt;
Workbench/TUI play locally; reboot receipts never prove playback.

`/voice` and `/call` share privacy-checked routing before publish/cache. AP
prefers VideoTTS unless `video_tts_supported: false`; otherwise use audio.
Groups are bounded and ordered. Audio failure holds video; fallback stays
group-local, stale tails cancel, late jobs only signal recovery, and audio stays
serial. See [language voices](HASHIKO_LANGUAGE_VOICES.md),
[Call/Phone policy](HASHI_CALL_AGENTPRESENCE_VIDEO.md#command-policy-2026-10-23),
[catalogue](HASHI_VOICE_TTS_CATALOG.md),
[routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md) and
[fallback](repairs/VOICE_PROVIDER_FALLBACK_REPAIR_20261014.md).

AgentPresence is owner-scoped/default-off; HASHI authenticates and freezes
Agent/Conversation. Only PAO-admitted human/client input triggers owner-route
feedback; AP-local input never echoes. `/phone` sends scoped leave/return; AP
owns presentation, HASHI services/voices. Ingress fails closed; surfaces remain
independent. See [input feedback](HASHI_AGENTPRESENCE_INPUT_FEEDBACK.md).

Deploys add Edge/Kokoro/SAPI without changing settings; the 957 MB portable keeps
Edge/SAPI/Piper, omits Kokoro/Torch. See
[deployment](repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

[Records](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki needs new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
