# HASHI — Roadmap

> High-level roadmap only. Keep it lightweight and current.

---

## Strategic Direction

HASHI is moving through three connected stages:

1. **Personal local agent project** — HASHI started as a vibe-coded,
   human-directed AI agent system for one owner running local agents, backends,
   voice, memory, scheduling, tools, and Workbench.
2. **Open-source orchestration platform** — HASHI now provides a broader
   multi-agent runtime with backend adapters, Nagare workflows, HER mode,
   Superloop operations, HChat, Remote, EXP guidebooks, and local-first
   automation.
3. **Enterprise-grade HASHI AAI** — the enterprise line turns the same codebase
   into an Agent as Interface control plane: governed profiles, identity,
   policy, approvals, audit, evidence, connectors, admin surfaces, and
   deployment artifacts for organizations that want self-hostable, inspectable,
   open-source agentic AI orchestration.

The goal is not to abandon the personal project roots. The goal is to preserve
smooth personal/local use while adding the governance and operational controls
needed for professional enterprise adoption.

Current version-line interpretation:

- **Enterprise AAI `v0.1.0-alpha.1` / package `0.1.0a1`** is the current
  enterprise alpha artifact-freeze line.
- **HASHI `v4.0.0-alpha.2`** is the current broader platform release candidate,
  led by the provider-neutral HASHI-native Python HER v2 runtime and its
  evidence-backed execution contracts.

---

## Enterprise AAI v0.1.0-alpha.1 (Current Enterprise Alpha)

HASHI AAI Enterprise `v0.1.0-alpha.1` is the current enterprise artifact-freeze
line. It resets the enterprise-grade package version to `0.1.0a1` while the
broader HASHI 4 line advances independently through the advanced HER
`v4.0.0-alpha.2` release candidate.

This enterprise alpha includes governed profiles, identity/SSO/SCIM primitives,
policy/approval/audit, connector MVPs, Workbench enterprise surfaces, and
Compose/Kubernetes/Helm/systemd/SIEM deployment artifacts for alpha operator
review. It is not production-certified; customer-like enterprise server, IdP,
SIEM, and Kubernetes/cloud validation remain post-alpha.

## v4.0.0-alpha.2 (Current Release Candidate)

The development accumulated after the first v4 foundation alpha is now
consolidated as the `v4.0.0-alpha.2` release candidate:

- provider-neutral HER v2 as the sole HER execution backend;
- Direct (`zero`), Strategic (`low`), and Planned (`medium`) production
  execution modes, independent from provider reasoning and tool-call count;
- persistent direct-conversation ordering, isolated scheduler execution,
  reply-target snapshots, explicit stream ownership, and idempotent delivery;
- explicit staged orchestration, Tool Gateway/MCP, secure multimedia, and
  optional agent-local Habit/Meditation;
- deferred redesign of the retained but non-public Adaptive, Reviewed, and
  Assured implementation;
- provider-aware native/fallback multimodal routing, configurable Single/Hybrid
  Quick and Pro routes, and the HASHI API provider;
- automatic context maintenance that continues selected-model execution when
  compaction is unavailable or insufficient;
- caller-owned OpenAI function tools through the Codex app-server bridge;
- retirement of HER v1, native Rust source/packages, `/rebuild` machinery,
  `claw-cli`, the legacy fixed runtime, and the OpenClaw importer;
- canonical Workbench Agent Overview and shared-token-authenticated remote
  terminal execution;
- crash-safe HER v2 WIP context with explicit preserve/inject/clear audit
  events, provider-only OpenRouter/DeepSeek selection, and exact DeepSeek native
  vision capability;
- client-neutral persistent Session/Message/Run/Event services behind a
  fail-closed qualification boundary, plus restart reconciliation for orphaned
  Runs; and
- three-state Telegram notification control with a Quiet mode that silences
  interim activity while keeping final, error, warning, recovery, and important
  messages attention-bearing.

The local Python, architecture, documentation, and publication-hygiene gates
must be rerun against each outgoing `main` tip. Wider runtime rollout and release
tagging remain separate operator-controlled actions rather than implicit effects
of a source push.

See [the release notes](RELEASE_NOTES_v4.0.0-alpha.2.md) for the delivered scope
and alpha boundaries. The
[2026-08-27 checkpoint](HASHI_UNRELEASED_CHECKPOINT_2026-08-27.md) records the
current integrated implementation and publication boundary. The
[2026-08-24 checkpoint](HASHI_UNRELEASED_CHECKPOINT_2026-08-24.md) and
[2026-08-13 checkpoint](HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md) remain
historical records of earlier integration states.

---

## v4.0.0-alpha.1

HASHI v4 alpha is the broader platform foundation line. It is focused on:

- **HER mode foundation** — `her` is integrated as a scoped backend with
  provider routing, packaged runtime discovery, checksum-checked manifest
  metadata, and live repo-root read/write/edit validation.
- **Grok CLI backend support** — `grok-cli` is integrated into the flex backend
  ecosystem with local CLI authentication, Grok 4.5 as the current default,
  `streaming-json` parsing, guarded empty-answer retry, and live
  controlled-probe validation. Empty-answer retry recovery is unit-covered;
  the post-reboot live probe did not naturally trigger that path.
- **Superloop operational foundation** — long-running controller loops now have
  an explicit function contract for taskboards, waits, HChat replies, evidence,
  issue handling, and closeout barriers.
- **Enterprise AAI strategy** — HASHI Enterprise is now framed around
  Agent as Interface: a governed, auditable, open-source control plane for
  human-AI work orchestration. The enterprise direction keeps one codebase with
  `personal`, `team`, and `enterprise` profiles: `personal` preserves the
  current one-owner model, while enterprise `individual_user` is a governed
  identity role rather than a top administrator. See
  [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md),
  [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md), and
  [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md). The
  ready-to-implement phase plan is tracked in
  [HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md](HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md).

Limitations recorded at the v4 alpha tag (later work is tracked in the
unreleased checkpoint above):

- Packaged `hashi-her` release binaries are not yet shipped.
- Claw Tool Gateway/MCP parity is planned, not complete.
- Claw shell/test execution and browser/web parity require later gates.
- Superloop is not yet a stable unattended automation product; alpha loops must
  preserve explicit waits, evidence, issue state, and inbox-drain closeout.

---

## v1.1 (Completed)

- `v1.1-debugging` is now considered **completed** (stabilization + semantics fixes).

---

## v2 Upgrade Roadmap — ALL COMPLETED ✅

> All v2 target outcomes have been delivered as of 2026-03-20.

### V2.1 — CLI-first continuity for execution backends ✅
- CLI backends (Gemini/Claude/Codex) now rely on their own continuous sessions for continuity.
- Bridge sends incremental prompts by default; no large compressed context blocks.
- Role/habits defined via CLI-native system mechanisms (`GEMINI_SYSTEM_MD`, `claude.md`, etc.).
- Bridge-managed transcript/handoff remains available but is explicit (user-triggered).

### V2.2 — Toolbox for OpenRouter/API agents ✅
- Full tool execution layer implemented for OpenRouter-backed agents.
- Model proposes tool calls → bridge executes locally → results returned → model continues.
- **11 built-in tools:**
  - `bash` — run shell commands (sandboxed, timeout + blocklist controls)
  - `file_read` — read files with offset/limit pagination
  - `file_write` — write/create files (size-capped)
  - `file_list` — list directories with glob filter and recursive option
  - `apply_patch` — apply unified diff patches (dry-run validated)
  - `process_list` — list running processes by name (requires `psutil`)
  - `process_kill` — send SIGTERM/SIGKILL to a process by PID
  - `telegram_send` — send Telegram messages by chat_id or HASHI agent_id
  - `http_request` — arbitrary HTTP calls (GET/POST/PUT/DELETE/PATCH)
  - `web_search` — Brave Search API (requires `brave_api_key` in `secrets.json`)
  - `web_fetch` — fetch any URL and return content as Markdown
- Tool access is per-agent via `tools.allowed` in `agents.json`. No `tools` key = backward compatible.

### V2.3 — Mode switching (fixed ↔ flexible) ✅
- `/backend` command switches an agent between fixed CLI backends and flex OpenRouter backends.
- Backend switching is atomic: not committed until a valid model is selected.
- Rollback exists: previous backend restored if new one fails to initialize.
- Flex backend state (active backend, selected model, effort level) persists in `state.json`.

### V2.4 — Interactive TUI wrapper ✅
- `tui.py` launcher provides a split-panel terminal UI wrapping `main.py` as a subprocess.
- Log panel (upper ~80%): real-time stdout/stderr streaming with auto-scroll.
- Chat input bar (lower ~20%): sends messages to agents via HTTP API Gateway.
- Agent selector and status bar (agent name, backend, uptime, gateway reachability).
- Built with [Textual](https://github.com/Textualize/textual); `main.py` unchanged.
- Graceful degradation when API Gateway unavailable.

---

## Additional Features Delivered (v1.2-alpha)

### /dream — Nightly Memory Consolidation
- Agents “dream” at 01:30 daily: LLM reflects on the day’s transcript, extracts memories into `bridge_memory.sqlite`, optionally updates `AGENT.md` with behavioral insights.
- Snapshot-based `/dream undo` for morning rollback (no LLM required).
- Persistent `dream_log.md` and on/off toggle via `tasks.json` cron.

### Process-Tree Force Stop
- `/stop` now kills the entire subprocess tree (not just the main PID) via `os.killpg()`.
- Prevents zombie child processes from holding stdout/stderr pipes open.

### Recovery Retry and Resend
- `/resend` replays the last model or Bridge output without inference.
- `/retry` persists the last retryable prompt, stops stale execution, creates a
  clean CLI/API context, restores recent handoff continuity, and reruns that
  prompt. Retry and resend state survive runtime restarts. See
  [RETRY_RESEND_COMMANDS.md](RETRY_RESEND_COMMANDS.md).

---

## Active Design Items

### Wrapper Agent Mode

Status: **implemented in v3.2.0; retired from the product surface in v4.0.0-alpha.2**.

Wrapper Agent Mode historically let a strong core model do the actual work
while a separate stateless wrapper model rewrote the final user-facing
tone/persona. HASHI now exposes only Fixed and Flex. Old mode state migrates to
the configured default, and the saved wrapper blocks remain only as historical
compatibility data.

Historical implemented scope:

- Merge-safe `state.json` writes preserve `core`, `wrapper`, and `wrapper_slots`.
- The former `/mode wrapper`, `/core`, `/wrap`, and `/wrapper` product controls now return a retirement notice.
- Foreground/background responses, listeners, transfer suppression, handoff, project chat, voice replies, and HChat reply summaries use wrapper-visible output where appropriate; active `bridge:hchat` sends remain bypassed until the delivery-boundary HChat pipeline is implemented.
- `/verbose on` shows compact wrapper status, latency, and fallback details without exposing raw answer drafts.
- `/reset CONFIRM` preserves wrapper configuration and prompt slots, matching `/sys` preservation behavior.

Design and acceptance record:

- `docs/WRAPPER_AGENT_MODE_PLAN.md`
- `docs/HCHAT_DELIVERY_BOUNDARY_PLAN.md`

### Background Jobs

Status: **planned / design accepted with revisions**.

Background Jobs will become HASHI's durable, session-aware execution primitive
for long-running operating-system work. It is planned as a future function-layer
upgrade, not a core rewrite and not a scheduler shortcut.

The design goal is to let agents and operators start long-running local or
remote jobs, receive a durable job id immediately, monitor stdout/stderr, cancel
safely, and receive completion notifications without blocking the conversation.

Key product boundaries:

- **Not cron/heartbeat/nudge** — scheduled triggers may launch a job, but the
  process instance is owned by Background Jobs.
- **Not LLM `background_mode`** — detached model generation remains separate
  from OS process supervision.
- **Not `/terminal/exec background=true`** — Remote background work requires a
  dedicated `background_jobs_v1` capability and target-instance policy.
- **Not generic `bash` with longer timeout** — jobs need durable state, bounded
  logs, ownership, cancellation, recovery, and audit.

Planned implementation shape:

- Layer 2 `BackgroundJobManager` as a function-layer service with a minimal
  kernel handle.
- SQLite-backed local job store for Phase 1.
- Per-agent partitions with cross-agent admin visibility.
- Explicit `/reboot` and full-restart recovery semantics.
- Workbench read-only job APIs early, alongside a thin `/bg` Telegram adapter.
- Remote support later through `background_jobs_v1`, using the target
  instance's policy and durable notification retry.
- Enterprise hardening later for project scoping, approval, audit, retention,
  quotas, and SIEM/webhook export.

This capability supports the broader HASHI AAI direction by turning HASHI from a
chat-driven agent runtime into a more reliable work orchestration control plane
while preserving personal/local use.

Design record:

- `docs/HASHI_BACKGROUND_JOBS_DESIGN.md`

---

## Deferred Research Items

### Telegram Bot API 10.1 Rich Messages

Status: **deferred / wait for stable client-library support**.

Telegram Bot API 10.1 added `sendRichMessage` in June 2026, including native
GitHub-Flavored Markdown and HTML tables. HASHI currently sends persistent chat
output through the established `sendMessage` plus basic HTML path; that path
cannot render Markdown tables, and the installed `python-telegram-bot 22.6`
release does not expose the new rich-message methods.

Current decision:

- Do not replace or destabilize the proven `sendMessage` path merely to gain
  early table support.
- Wait for a stable public `python-telegram-bot` integration, unless a stronger
  product need later justifies a small audited direct Bot API adapter.
- Treat valid model-generated Markdown tables as a transport capability gap,
  not as a model failure.

Future upgrade target:

- Add `sendRichMessage` as an optional Telegram transport capability shared by
  all Flex Agent execution modes, while retaining `sendMessage` as the
  universal fallback.
- Route compact structured content to `rich_message.markdown`; convert
  narrative or mobile-unfriendly wide tables into readable vertical cards.
- Preserve delivery ordering, retry/failover, notification policy, exact
  generated-text audit, transport receipts, and truthful failure reporting.
- Use persistent `sendRichMessage` first; consider ephemeral
  `sendRichMessageDraft` streaming only as a separate later enhancement.
- Add capability probing and end-to-end tests for native table delivery,
  unsupported-library/API fallback, long-cell adaptation, and Telegram receipt
  verification before rollout.

Revisit when the Python client exposes Bot API 10.1 Rich Messages through a
stable public API, or when Telegram table readability becomes important enough
to justify a maintained direct adapter.

Official reference:
[Telegram Bot API — Rich Messages](https://core.telegram.org/bots/api#rich-messages).

---

### Complete Structured Audit Log For Slash Commands

Status: **deferred / revisit later**.

Current state:

- HASHI can audit many queued model-backed requests through existing sources such as `token_audit.jsonl`, transcript files, and runtime logs.
- Telegram slash commands are registered through command handlers, so command usage is not yet captured as one complete, durable, command-level audit stream.
- This means some command-derived activity can be inferred later, but exact usage statistics for all slash commands are not guaranteed today.

Deferred target:

- Add a structured audit event for every slash command invocation with at least:
  - `timestamp`
  - `agent`
  - `command`
  - `args`
  - `user`
  - `outcome`
- Make this audit path apply to every supported Flex Agent execution mode.
- Keep the log durable and machine-readable so command-frequency reports, unused-command reports, and per-agent command analysis can be generated exactly rather than inferred.

Why deferred:

- This is observability and audit hardening work, not an immediate functional blocker.
- It should be designed once at the command-binding/runtime boundary rather than added piecemeal to individual commands.
- It should land together with a clear reporting contract so future statistics are exact and stable.

Revisit when:

- We want exact slash-command usage statistics across the system.
- We are ready to standardize a command audit schema and retention location.
- We want product-grade reporting for command adoption, dead commands, and operator behavior.

---

### WhatsApp Real-Time Voice Calls For HASHI

Status: **deferred / revisit later**.

We investigated adding a real WhatsApp call interface to HASHI, using the already-linked WhatsApp account on HASHI1 rather than WhatsApp Business or OpenAI Realtime.

What we explored:

- A local WhatsApp Desktop call bridge on HASHI1.
- A Windows-native helper process because HASHI runs in WSL2 while WhatsApp Desktop UI and audio devices live on Windows.
- Incoming-call detection through:
  - Windows UI Automation,
  - window-title/process probing,
  - screenshot diagnostics,
  - OCR fallback,
  - missed-call evidence.
- A future audio path with VB-CABLE/VoiceMeeter, local VAD/STT/TTS, and existing HASHI agent routing.

What we learned:

- WhatsApp Desktop can receive the real incoming call on HASHI1.
- Current WhatsApp Desktop call detection is not reliable enough for a stable HASHI feature.
- UI Automation mostly sees the WhatsApp WebView shell, not the useful call/chat content.
- Screenshot/OCR can help with diagnostics, but it still depends on an unlocked interactive Windows desktop.
- Lock screen, minimized/background behavior, app updates, and WebView layout changes make this route fragile.
- The current WhatsApp message bridge cannot be simply extended into a call bridge. WhatsApp voice calls require a different real-time calling stack, including WebRTC/media/signaling/encryption behavior, and self-implementing that is not a practical HASHI roadmap item.

Current decision:

- Do **not** continue WhatsApp Desktop real-time calls as a near-term implementation path.
- Keep the research and experimental code as a reference for later.
- Treat the current route as a possible future playground/demo only, not a production-grade unattended call feature.

Revisit when:

- HASHI has spare development time for experimental local desktop automation.
- WhatsApp Web/Desktop exposes more stable browser-call controls that can be handled through Chrome/CDP.
- A suitable official WhatsApp call media API becomes available for the intended account type.
- We choose to use ordinary phone numbers through a provider such as Twilio Programmable Voice.

Preferred future direction:

- For reliable unattended voice calls, use a normal phone number and a voice provider transport.
- Keep HASHI's voice runtime transport-agnostic so WhatsApp, phone, local microphone, or future official call APIs can share the same VAD/STT/TTS/agent pipeline.

---

### Stable Public Tunnel for OLL Browser Bridge (`oll.barryli.phd`)

Status: **deferred / revisit later**.

We set up a Cloudflare Tunnel (`hashi-oll`) to expose the OLL browser gateway at `oll.barryli.phd`, pointing to `http://127.0.0.1:8876` on HASHI1. DNS was fully propagated and the tunnel was verified working.

What we completed:

- Cloudflare Tunnel `hashi-oll` created (ID: `e04e2f57-d337-4a6c-96b8-144a77e23c5a`).
- DNS CNAME `oll.barryli.phd` → tunnel, confirmed propagated on public resolvers.
- `barryli.phd` nameservers moved to Cloudflare (achiel/val).
- `cloudflared` successfully ran as a background process on HASHI1 with `http2` protocol fallback.
- Blog (`barryli.phd`) verified unaffected throughout.

What still needs to be done:

- `cloudflared` tunnel credentials were not persisted; the process stopped after WSL restart.
- Re-authenticate: run `cloudflared login` to regenerate `~/.cloudflared/cert.pem`, then retrieve the permanent token via `cloudflared tunnel token hashi-oll`.
- Set up `cloudflared` as a persistent systemd/openrc service on HASHI1 so it survives restarts.
- Start `browser_gateway` (port 8876) as a persistent service alongside HASHI instead of a manual background process.

Revisit when:

- There is time to properly set up `cloudflared` as a system service.
- OLL browser bridge usage is needed for stable off-LAN access.

---

## Notes
- This roadmap is outcome-based; implementation details live in dedicated design docs.
- Design docs: `docs/V2.2_TOOL_EXECUTION_PLAN.md`, `docs/HASHI_VOICE_BRIDGE_PLAN.md`, `docs/WRAPPER_AGENT_MODE_PLAN.md`, `docs/HASHI_BACKGROUND_JOBS_DESIGN.md`
