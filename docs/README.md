# HASHI Documentation

This directory contains developer documentation and operational notes for HASHI.

> **Status:** v3.2-alpha — slim core architecture accepted, Wrapper Agent Mode implemented, browser gateway and runtime hardening active.
>
> **Changelog:** see [`../CHANGELOG.md`](../CHANGELOG.md) · **Roadmap:** see [ROADMAP.md](ROADMAP.md).

---

## Active (keep these current)

### Core references
- [AGENT_FYI.md](AGENT_FYI.md) — Bridge environment FYI (operational reference)
- [initial.md](initial.md) — Onboarding prompt template (onboarding-only)
- [tools.md](tools.md) — Tools & operations reference
- [WORKBENCH_NOTES.md](WORKBENCH_NOTES.md) — Workbench/runtime semantics
- [HASHI_SLIM_CORE_ARCHITECTURE.md](HASHI_SLIM_CORE_ARCHITECTURE.md) — v3.2 slim core architecture and hot manager rebuild contract
- [HASHI_CORE_SLIMMING_PLAN.md](HASHI_CORE_SLIMMING_PLAN.md) — implementation plan and acceptance record for the slim core migration
- [WRAPPER_AGENT_MODE_PLAN.md](WRAPPER_AGENT_MODE_PLAN.md) — implemented v3.2-alpha wrapper agent mode design, command model, state contract, and acceptance record
- [HASHI_VOICE_BRIDGE_PLAN.md](HASHI_VOICE_BRIDGE_PLAN.md) — local-first voice runtime plan for the WhatsApp Desktop call bridge and future provider transports

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
