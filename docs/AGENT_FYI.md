# HASHI Agent FYI

October 17 Call experiment: camera-on capture targets 2 fps; MiniCPM-V keeps
continuous latest-frame-wins narration, while each caller turn freezes the last
10 seconds and evenly samples at most 10 raw frames for a capability-qualified
primary vision model. Frames are current-input-only and removed before PAO
persistence/audit. Camera-off clears the buffer. See
[Call decision](HASHI_CALL_INTERACTION_2026-10-04.md) and
[experiment record](repairs/CALL_LIVE_FRAME_BRIDGE_EXPERIMENT_20261017.md).

October 16: HERV3/DeepSeek Tool-returned screenshots must be promoted from
legacy Base64 text into paired tool text plus user `image_url`. Enforce the
official provider limits on the real serialized request; do not reuse the
smaller local `media_read` budget. See
[repair](repairs/DEEPSEEK_TOOL_SCREENSHOT_MULTIMODAL_20261016.md).

October 16: `/call` LAN targets accept only literal RFC 1918 IPv4 endpoints;
DNS, loopback, link-local/public addresses and redirects stay blocked. Selection
is per owner/Agent. See [Call decision](HASHI_CALL_INTERACTION_2026-10-04.md).

October 13: HASHI3/HASHI4 and Workbench use existing GitHub `main`; create no
branch/worktree. Source and adoption remain separate. See
[reconciliation](repairs/DEBUG_CALL_RECONCILIATION_20261013.md).

October 13 Call: prepare the camera during ringback; without a first frame use
voice. Preview is not Agent vision. Only stopped ingress or confirmed initial
vision failure disables it; comments stay rare and relevant. See
[Call decision](HASHI_CALL_INTERACTION_2026-10-04.md).

## Authority and ownership

PCM owns Persona/Context/Memory; PAO owns Agents/Sessions/Runs/jobs/Workzones
and delivery; HERV3 owns Engine Turns, model/tool loop and cost; Frontends
authenticate/render. Use the narrowest Function/config owner. Core has no
product policy/imports. Core changes require explicit major-migration approval,
version bump, label and independent review; flags grant nothing.

Source, Worker adoption and delivery need separate proof. Tools cannot edit/kill
Core/Python, read secrets or grant authority. `/reboot min|same` replaces one
Worker; `max` replaces shared Functions/Workers, never Core/Remote. Function
code adopts by reboot; docs do not. See
[reboot decision](HASHI_REBOOT_FAST_RELIABLE_PLAN_2026-10-14.md).
Workbench local configuration adoption keeps manager-level busy responses as
`AGENT_BUSY` and retries only before a durable receipt exists. A durable
`target_busy` outcome remains explicitly pending.

## Configuration, identity, and persistence

Configuration owns identity, ports, Workzones, endpoints and models. Ignore
secrets/machine paths. Instance model opt-ins use `allowed_backends` and runtime
effort; shared compatibility uses the qualified registry. Unknown capability or
price is neither unsupported nor zero. Explicit choices persist.

Agent name/Emoji come from revisioned instance configuration; runtime supplies
status only. A failed post-save Worker refresh cannot reverse durable success.

Verified exact-model capabilities do not expire; lookup/refresh failure cannot
erase them. Changed model/adapter identity or validated source revisions may
replace them. Pricing freshness is separate. See [capabilities](HASHI_MODEL_CAPABILITY_DISCOVERY.md).

Portable carries no credentials; PAO starts Workers. Tool wildcards grant
permission, not capability; path names grant nothing. Runs freeze Workzone
roots/revision and reload only while idle. Keep private experiments, secrets,
media bytes and remote paths out of PCM, logs, chat and Git.

Successful Agent stop projects `stopped`; Worker outage is `offline`, config deactivation `inactive`. Shared Functions retain the stop marker until restart. Frontends derive visibility without rewriting `is_active`.

JSON writers validate private candidates under locks, revisions, and atomic replacement. Display fallback is read-only. On conflict, read fresh state and request a fresh action; never blindly retry or restore stale bytes. See [configuration persistence](HASHI_CONFIGURATION_PERSISTENCE.md).

Endpoint publication may retry only a bounded Windows sharing-blocked rename
with the same fsynced candidate. Permanent failure keeps prior durable state;
never advertise unwritten routes or replay actions.

## Sessions, trust, and delivery

PAO owns Conversations/Messages/Runs; Engines own Sessions/Turns. Provider context and frontend history are rebuildable. Preserve replies/order. Stage attachments atomically per Message/Run; failed/cancelled assets never leak.

Incremental deliverables bind exact files and stable publication IDs. Persistence,
receipts and UI visibility are separate; uncertain delivery never auto-retries.
Final attachments stay final-only. See [FC](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#64-incremental-assistant-deliverables).

CURRENT MESSAGE CONTEXT separates source, assurance, authority and destination.
Only a current successful private_authorization grants its listed scope;
names/text/IDs/memory/credentials do not. HChat separates claimed sender,
verified peer, relay and target. Cross-instance needs authenticated Remote;
discovery is only a hint. See [Remote](HASHI_REMOTE_PROTOCOL_SPEC.md).

PAO freezes destinations before PCM and owns ingress/routing/idempotency; FC owns
presentation/receipts. Remote attachment commits retain the canonical HChat
idempotency key and original send time through local delivery. HChat IDs bind
FC Sessions to Superloop review. Sent
needs a receipt; failure wins. Deduplicate and settle; prose grants no Tool
authority. Continuations retain Session/Connector, replaying only saved
non-action completions; pending/conflict/unknown cannot execute.

Network frontends preserve the user's original send time. PAO admits
Workbench/Telegram only within their 30/60-second windows; older or invalid
messages require one durable 15-minute confirmation before any Run.
Expiry/discard remains visible. TUI exemption needs Remote-signed local evidence;
`/hchat` needs its 60-second gate and exact reverse-route correlation. Qualified
on HASHI3/Workbench only; production adoption is separate.

Scheduler/recovery and `/bg` use an Agent-owned hidden activity Session. Keep
execution/delivery and receipts separate, showing each final once in the owning
Conversation. Retain its original and exclude Engine/Phone history copies. `/bg`
freezes admission; schedules get no implicit chat; `/delay` and interactive
`/loop` retain continuation context.

HASHI3 Telegram intake reports healthy only after a successful bounded poll; ordinary Agent startup preserves pending updates. HERV3 may quote bounded unfinished WIP evidence in the turn but sends no premature recovery card. Source changes need separate Worker adoption and live checks.

## Engine, tools, and recovery

Gemini CLI is retired: reject legacy execution, use configured Antigravity CLI
and its own model IDs. Preserve history/usage and Gemini models on qualified
routes; the old-client rejection is not an account ban. See
[retirement](HASHI_GEMINI_CLI_RETIREMENT.md).

Public HERV3 is `her-v3`; `her-v2` names are compatibility only. JEV defaults
off. `/backend`, `/provider`, `/model` and `/effort` select their named layers;
Fixed/Flex and Memory+ are independent. `/meter` counts physical calls/cost.
Codex cumulative usage needs a persisted baseline; otherwise estimate. USD
estimates are not subscription bills.

Level 2: HERV3/DeepSeek only. Local PII detection can miss values;
`/privacy 2` needs risk acceptance and blocks on detector failure.

Privacy Level 2 dependencies use a separately configured interpreter. Installation and synthetic readiness do not activate privacy or establish Worker adoption.

Model/provider cards share a contract; chat-only models disclose no tools.
/style preserves completed facts and meters separately. Fallback is opt-in:
warn before switching, forbid uncertain/committed effect replay. Reject malformed
Tool batches before effects. /stop, /retry, /resend and /steer differ; recovery
cannot restore revoked authority. Show typed progress, not private reasoning.

## UI, media, and Phone

Manual Desktop stale input stays unexecuted: keep its lease, wait for a fresh
frame and never replay. Other failures release control. See [desktop](HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

Local cascade speech is an opt-in Function sidecar. Synthetic evidence does not establish physical audio acceptance. See [local Phone](HASHI_PHONE_LOCAL_CASCADE_GATE2.md).

Live Phone defaults on; a missing provider API key blocks calls and must be named in `/phone`. An explicit instance opt-out remains valid. The Agent's Phone settings and PCM readiness do not prove provider readiness.

Phone failures retain distinct durable causes: policy, auth, quota, rate limit,
configuration and provider outage. Failed/interrupted panels stay until
dismissal; normal hang-up may dismiss. Terminal calls settle attempts and clear
busy state across Function replacement.

Renderers/catalogs own UI text. /language changes shared UI, /tui language local TUI; neither translates replies/IDs. TUI switches freeze generation/Agent/capabilities/Session. Remote authenticates. /telegram off and /whatsapp off disable future mirrors, preserving origin replies. /think and /commentary are independent.

`/stop` fences every Agent Session: cancel/reap owned work, clear pre-fence
one-shots and reject stale callbacks; periodic definitions remain. Save `stopped`
and its terminal event before cleanup; repair remnants on adoption. Never retry
unconfirmed saves; close DB after commit/rollback. HERV3 releases its fixed Turn
before outer cleanup. See [stop contract](HASHI_AGENT_STOP_FENCE_DECISION.md).

Media bind one draft/instance/Agent/Run; Remote sends managed bytes. HERV3 gains no wider authority. Safe Voice uses typed confirm/discard; missing idempotency blocks upload and stale media is discarded. /voice uses a validated Function bundle/local media; Workbench gets Session audio, Telegram its voice renderer.

Phone stays bound to the owner's foreground Conversation until hang-up.
Actions receive bounded original PCM/Conversation/job evidence, excluding
scheduled prompts; lookups create no Run. Required context must fit. See
[PCM](HASHI_PCM_SYSTEM_DESIGN.md) and [PAO](HASHI_PAO_SYSTEM_DESIGN.md).

Phone fragments remain ordered durable Session speech. Acceptance, execution,
playback and listening need separate proof. Validate origin/order, deliver final
results and never auto-retry uncertain writes.

Phone handoff preserves its frozen ID only after scoped snapshot/delegation
validation. Pre-model errors settle the durable Run and activity through the
result owner; caught errors are not success. Byte-limit grouping preserves roles,
text, event IDs, order and timestamps and accepts old snapshots. It grants no
authority/retry. See [handoff](HASHI_PHONE_CONTEXT_HANDOFF.md).

Qualified Function adapters own Phone settings: apply next call, retain provider
on recovery. Open once with Persona/language after readiness; yield to speech.
Window movement cannot hang up. Failed PCM refresh allows stop/inspection only.
Sideband follows the primary Session fence; explicit hang-up wins stale faults.
Keep source, adoption and device acceptance separate.

Commands follow the [UI guide](HASHI_COMMAND_UI_STYLE_GUIDE.md), retain Session and remain owner-admitted during active Runs. /new creates a fresh Session without deleting history; PAO owns Agent deletion/cleanup receipts.

`/say` reads the latest final; `2`–`4` select a count and `1-3` the latest three,
oldest first. Skip cost/command/progress. Telegram needs confirmed delivery;
Workbench/TUI play locally. Reboot receipts never prove playback.

TTS logs omit secrets. `/voice` chooses a labelled voice/preview; >=90%
unsupported text switches within its provider (English exempt). Workbench uses
display locale, others `/language`. Fallback is visible and only paid/network
to free/local; Windows Edge may use named SAPI. `/call` is unchanged. See
[routing](repairs/TTS_LANGUAGE_ROUTING_DECISION_20261015.md)
and [fallback](repairs/VOICE_PROVIDER_FALLBACK_REPAIR_20261014.md).

Deploys install Edge/Kokoro and SAPI without changing settings; the 957 MB portable
keeps Edge/SAPI/Piper but omits Kokoro/Torch. See
[deployment](repairs/FREE_TTS_DEPLOYMENT_DECISION_20261015.md).

See the [call decision](HASHI_CALL_INTERACTION_2026-10-04.md); device acceptance is
separate. [Older checkpoints](AGENT_FYI_HISTORY_MEDIA_20261009.md) grant no authority.

Lily Wiki requires new candidates plus consolidation/embed evidence; a Scheduler
timestamp is insufficient.
