# HASHI Documentation

This directory contains developer documentation and operational notes for HASHI.

> **Status:** HASHI AAI Enterprise v0.1.0-alpha.1 is the current enterprise
> alpha target. It is deployment-artifact-ready for alpha testing, with full
> enterprise-server production validation pending.
>
> **Changelog:** see [`../CHANGELOG.md`](../CHANGELOG.md) · **Roadmap:** see [ROADMAP.md](ROADMAP.md).

---

## Start Here

HASHI began as a personal, local, vibe-coded AI agent project and is now
evolving into **HASHI AAI**: an open-source Agent as Interface control plane for
professional, governed human-AI work orchestration.

Use these docs by intent:

- **Understand the product direction:** start with
  [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md)
  and [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md).
- **Understand the current alpha boundary:** read
  [HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md](HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md)
  and
  [RELEASE_NOTES_HASHI_AAI_ENTERPRISE_v0.1.0-alpha.1.md](RELEASE_NOTES_HASHI_AAI_ENTERPRISE_v0.1.0-alpha.1.md).
- **Understand the long-term route:** read [ROADMAP.md](ROADMAP.md) and
  [HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md](HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md).
- **Operate the current alpha artifacts:** read
  [HASHI_ENTERPRISE_DEPLOYMENT.md](HASHI_ENTERPRISE_DEPLOYMENT.md) and the
  enterprise runbooks linked from the readiness review.

The documentation should be read with one important boundary: Enterprise AAI
`v0.1.0-alpha.1` is artifact-ready for alpha testing, not production-certified.
Personal/local HASHI use remains the stable default path while enterprise
deployment validation continues.

---

## Active (keep these current)

### Core references
- [AGENT_FYI.md](AGENT_FYI.md) — Bridge environment FYI (operational reference)
- [STEER_COMMAND.md](STEER_COMMAND.md) — Telegram `/steer` mid-task course correction (busy wrapper vs idle plain text, error suppression)
- [initial.md](initial.md) — Onboarding prompt template (onboarding-only)
- [tools.md](tools.md) — Tools & operations reference
- [WORKBENCH_NOTES.md](WORKBENCH_NOTES.md) — Workbench/runtime semantics
- [HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md](HASHI_ENTERPRISE_AAI_VALUE_PROPOSITION.md) — Agent as Interface value proposition and enterprise positioning
- [HASHI_ENTERPRISE_AAI_PRD.md](HASHI_ENTERPRISE_AAI_PRD.md) — Enterprise AAI product requirements and development plan
- [HASHI_ENTERPRISE_PROFILE_ADR.md](HASHI_ENTERPRISE_PROFILE_ADR.md) — accepted decision for one codebase, `personal`/`team`/`enterprise` profiles, and enterprise identity roles
- [HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md](HASHI_ENTERPRISE_AAI_IMPLEMENTATION_ROADMAP.md) — Enterprise AAI phase plan, `0.1 Alpha` cut line, tickets, dependencies, and migration matrix
- [HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md](HASHI_ENTERPRISE_AAI_READINESS_REVIEW.md) — Enterprise AAI `0.1 Alpha` readiness decision, evidence, deferred work, and completion boundary
- [HASHI_ENTERPRISE_DEPLOYMENT.md](HASHI_ENTERPRISE_DEPLOYMENT.md) — Enterprise `0.1 Alpha` deployment skeleton and current limits
- [HASHI_SLIM_CORE_ARCHITECTURE.md](HASHI_SLIM_CORE_ARCHITECTURE.md) — v3.2 slim core architecture and hot manager rebuild contract
- [HASHI_LAYERED_RUNTIME_BOUNDARIES.md](HASHI_LAYERED_RUNTIME_BOUNDARIES.md) — four-layer HASHI boundary: protected core, hot-reloadable functions, platform config, and instance config
- [HASHI_CORE_SLIMMING_PLAN.md](HASHI_CORE_SLIMMING_PLAN.md) — implementation plan and acceptance record for the slim core migration
- [WRAPPER_AGENT_MODE_PLAN.md](WRAPPER_AGENT_MODE_PLAN.md) — implemented v3.2 wrapper agent mode design, command model, state contract, and acceptance record
- [API_GUIDE.md](API_GUIDE.md) — OpenAI-compatible API Gateway guide, including per-instance gateway port rules
- [HASHI_XAI_API_BACKEND_PLAN.md](HASHI_XAI_API_BACKEND_PLAN.md) — xAI/Grok API backend design using Hermes-managed OAuth refresh and OpenAI-compatible gateway routes
- [HASHI_XAI_CLAW_OAUTH.md](HASHI_XAI_CLAW_OAUTH.md) — **Coming soon:** direct Grok OAuth (HASHI device login → Claw; no Hermes, no grok-cli). Code landed; live use needs HASHI's own xAI `client_id`
- [ANATTA_MIGRATION_PLAN.md](ANATTA_MIGRATION_PLAN.md) — Anatta live self-assembly architecture and validation plan
- [HASHI_REMOTE_FILE_TRANSFER.md](HASHI_REMOTE_FILE_TRANSFER.md) — direct cross-PC file push support for release artifacts and EXP transfer
- [HASHI_REMOTE_FILE_TRANSFER_AND_ATTACHMENTS_PLAN.md](HASHI_REMOTE_FILE_TRANSFER_AND_ATTACHMENTS_PLAN.md) — planned shared-token file transfer upgrade and message attachment design
- [SUPERLOOP_PLAN.md](SUPERLOOP_PLAN.md) — draft architecture plan for a long-running loop controller above Nagare
- [SUPERLOOP_FUNCTION_CONTRACT.md](SUPERLOOP_FUNCTION_CONTRACT.md) — runnable Superloop operational contract for schema, waits, HChat replies, evidence, and closeout
- [HASHI_REMOTE_SIDE_PROGRAM_UPGRADE_PLAN.md](HASHI_REMOTE_SIDE_PROGRAM_UPGRADE_PLAN.md) — comprehensive plan to make Hashi Remote a supervised side program with backward-compatible rescue
- [HASHI_REMOTE_P2P_UPGRADE_PLAN.md](HASHI_REMOTE_P2P_UPGRADE_PLAN.md) — Remote peer-to-peer messaging plan, function-layer implementation status, and rollout/adoption checklist
- [HASHI_REMOTE_RESCUE_PROTOCOL.md](HASHI_REMOTE_RESCUE_PROTOCOL.md) — Remote sidecar survival model and fixed HASHI start/status rescue endpoints
- [HASHI_REMOTE_PLATFORM_PROFILES_PLAN.md](HASHI_REMOTE_PLATFORM_PROFILES_PLAN.md) — platform differentiation plan for WSL, Windows sidecars, WatchTower, LAN peers, and validation aliases without forking `main`
- [HASHI2_WSL_STABLE_PORT_ROLLOUT_PLAN.md](HASHI2_WSL_STABLE_PORT_ROLLOUT_PLAN.md) — staged HASHI2 WSL rollout and full-function validation plan for stable Remote port allocation
- [AUDIT_VIBE_CODING_SUPERLOOP.md](AUDIT_VIBE_CODING_SUPERLOOP.md) — end-to-end vibe-coded product superloop with mandatory independent reviews and live runtime exit gates
- [HASHI_VOICE_BRIDGE_PLAN.md](HASHI_VOICE_BRIDGE_PLAN.md) — local-first voice runtime plan for the WhatsApp Desktop call bridge and future provider transports
- [CLAW_TOOL_GATEWAY_TELEMETRY_PLAN.md](CLAW_TOOL_GATEWAY_TELEMETRY_PLAN.md) — long-term plan for packaging Claw into HASHI, exposing internet tools through Tool Gateway/MCP, and structured thinking/token telemetry
- [CLAW_CODE_MODULE_PLAN.md](CLAW_CODE_MODULE_PLAN.md) — Claw module packaging notes, runtime binary expectations, and local release-binary evidence

### Nagare Flow System (v2.1)
- [NAGARE_FLOW_SYSTEM.md](NAGARE_FLOW_SYSTEM.md) — Complete technical reference for the multi-agent workflow orchestration engine
- [MIGRATION_FROM_HASHI.md](MIGRATION_FROM_HASHI.md) — Current extraction boundary and host responsibilities
- [HANDLER_GUIDE.md](HANDLER_GUIDE.md) — Step handler protocol and implementation guidance
- [ADAPTER_GUIDE.md](ADAPTER_GUIDE.md) — Host integration guidance for downstream adapters
- [LOGGING.md](LOGGING.md) — Stable event and snapshot contract
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) — Release-readiness gate for package and editor
- [KNOWN_LIMITATIONS_NAGARE.md](KNOWN_LIMITATIONS_NAGARE.md) — Current standalone limitations
- [RELEASE_NOTES_NAGARE_v0.1.0.md](RELEASE_NOTES_NAGARE_v0.1.0.md) — Initial extracted-package release notes
- [KASUMI_MCP_SERVER_PLAN.md](KASUMI_MCP_SERVER_PLAN.md) — Architecture and upgrade plan for a unified Kasumi MCP server spanning Nexcel, Wordo, and future modules

### Installation
- [INSTALL.md](INSTALL.md) — Standard installation guide
- [MACOS_INSTALL.md](MACOS_INSTALL.md) — macOS-specific installation

### Roadmap
- [ROADMAP.md](ROADMAP.md) — Version history + future plans

### Debugging
- [DEBUGGING.md](DEBUGGING.md) — Debugging guide and troubleshooting
- [KNOWN_ISSUES.md](KNOWN_ISSUES.md) — Known issues (keep minimal)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — Troubleshooting guide

### Release Notes
- [RELEASE_NOTES_HASHI_AAI_ENTERPRISE_v0.1.0-alpha.1.md](RELEASE_NOTES_HASHI_AAI_ENTERPRISE_v0.1.0-alpha.1.md) — Enterprise AAI v0.1 alpha release notes and known limits
- [RELEASE_NOTES_v4.0.0-alpha.1.md](RELEASE_NOTES_v4.0.0-alpha.1.md) — v4 alpha release notes for Claw mode and Superloop foundation
- [RELEASE_NOTES_v1.1.md](RELEASE_NOTES_v1.1.md) — v1.1 release notes

---

## Archive

Historical debug sessions, plans, and one-off fix docs:
- `docs/ARCHIVE/`

---

## Repo-level user docs

User-facing docs also exist at the repo root:
- `README.md`
- `INSTALL.md`
- `CHANGELOG.md`
- `LICENSE`
