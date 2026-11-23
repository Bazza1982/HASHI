# HAL v0.1 — Local Agent Handover (2026-11-23)

> Development handover only: NOT approval to merge, restart or deploy production.

## Branches and normative documents
HASHI: Bazza1982/HASHI — feat/hal-runtime-v01-20261123
Workbench: Bazza1982/hashi-workbench-v2 — feat/hal-canvas-kasumi-retirement-20261123

Both: docs/HAL_SPEC_v0.1.md (language author's exact vocabulary, not implementer's invention).
HASHI: docs/HAL_PRD_HASHI_v0.1.md, AGENTS.md, ARCHITECTURE.md, docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md, docs/TESTING_POLICY.md, docs/PURE_IDIOT_TEST.md.
Workbench: docs/HAL_PRD_WORKBENCH_v0.1.md, AGENTS.md, docs/TESTING_POLICY.md, docs/PURE_IDIOT_TEST.md.

## What is ALREADY on the branches
- HAL v0.1 spec and side-specific PRDs are committed.
- HASHI hal/language.py + hal/__init__.py + tests/test_hal_language.py are a PURE, no-side-effect compiler/matcher/variable-binding foundation. They have NOT been tested in a local checkout yet; they do NOT subscribe or execute.
- Workbench initial hal Canvas node, server type allowance, Inspector editor, translated shelf action, and removal of the fixed HEA half-screen launcher are committed. Browser/build/storage roundtrip unverified.
- Legacy HEA BACKEND remains. Some existing enabled rules may still run invisibly after removing the panel: THIS IS A RELEASE BLOCKER until migrated or controllable.
- Kasumi from Canvas is NOT YET removed. Never claim retirement complete until source/use and test sweep are finished.

## Your objectives (follow order; no scope creep)
A. BASELINE & AUDIT. Work in these two branches only, isolated worktrees. First read specs and AGENTS.md. Run existing HAL focused tests, validate protected-Core unchanged, locate affected Canvas tests, and correct starter code problems with tests. Do not silently add grammar.

B. HASHI RUNTIME. Create optional, independent Layer-2 HAL Function with parser/validator -> matching -> persistent execution context -> injected existing event/command adapters. Do NOT modify or import into Protected Core, modify Agent loop, add another LLM/scheduler, or change existing default readiness. HAL absent/disabled = HASHI normal. Implement owner/instance-scoped wildcard, per-run @agent_trigger, independent full stop (.), dependent semicolon (;), causal command/event correlation, idempotent receipts/recovery, #system_idle edge semantics (no busy-poll loops). Real APIs only: event feed currently proves completed/failed; commentary, deploy_finish, reboot_ok must be sourced/implemented or marked unsupported, never faked. /hashideploy must be verified before it is dispatchable. /reboot max admission != reboot success. Existing authorization and HMAC apply; avoid new permissions or complex gates. Explicitly reject undefined else and $stop semantics until author resolves.

C. WORKBENCH CANVAS. Finish standard HAL 工作流构件, Inspector-only script editor and optional compact visualization. No large fixed panel, no parallel Event Automation editor/executor, no new always-on UI, no global HASHi/Agent online gate. Offline Canvas edit/save/reload/undo/redo/import/export must work. HASHI HAL may be unavailable: show HAL-specific nonblocking unavailable status, never a fake-green execution. Connect validate/activate/status through existing Remote HMAC, no competing JS runtime/parser. Resolve Canvas workflow package boundary (Task/Approval only) honestly; no silent success when HAL nodes aren't executed.

D. LEGACY HEA DATA AND ACTIVE RULES. Audit enabled legacy rules, journal, cursors, jobs, and idempotency receipts BEFORE removing all management. Supply reversible migration or a temporary nonintrusive management path for currently active rules, with one executor per rule, no accidental replay on resume and no lost user data. Remove legacy backend only after completion and parity; do not leave hidden live triggers.

E. KASUMI RETIREMENT, FROM CANVAS ONLY. Inventory usages in CanvasNodeShelf.jsx, CanvasMode.jsx, CanvasInspector.jsx, KasumiEditorDialog.jsx, CanvasOfficeImportDialog.jsx, CanvasWordImportDialog.jsx and associated hooks, routes, previews, CSS, tests, locales, docs. Remove Canvas-only WORDO/Nexcel creation/import/edit/shelf and all orphaned references. Keep generic Artefact, Result, Task, approvals, Office attachments and other non-Canvas functionality. Do not delete existing users' Kasumi artefacts, documents, stored bytes or shared Kasumi consumers. Verify accessible non-Canvas read/export for legacy data; if no safe path, document blocker rather than destructively deleting. Update README/AGENTS/docs where Canvas Kasumi support is mentioned.

F. VALIDATE WITH PIT. In isolated non-production HASHI+Workbench processes, distinct state/ports, no HASHI4:
1. No HAL node / HAL unavailable: normal HASHI runs, Canvas, Remote Desktop, Control Panel, Settings and unrelated services work.
2. HAL node add/edit/close/save/reload/undo/redo/import/export, real desktop 1920x1080 / 1366x768 / mobile screenshots; zero extra Canvas occupancy when unused.
3. Two parallel wildcard sources bind distinct @agent_trigger, no duplicate event dispatch or accidental cross-instance identity.
4. Sequential semicolon chain DOES NOT advance from irrelevant events; independent full stop rules fire independently; engine restart preserves durable state.
5. Unsupported source events/commands, invalid grammar and uncertain command success all fail visibly, no hallucinated execution.
6. Kasumi Canvas gone without lost documents or broken non-Canvas tools; no invisible active HEA jobs, no second executor.
7. Protected Core fingerprint unchanged, focused tests with exact counts and SKIP/FAILs, genuine HTTP browser E2E, no production deployment.

## PIT / authority / reporting
Enforce Stupid UI (no half-screen panels, wasted space or fixed resize), Stupid Dependencies, Repetition, Logic, Switches, Tests, Copy and Safety. HAL is useful when present, never required for unrelated functions. No new language constructs unless the author personally approves. No general workflow rewrite.

Open/update two LINKED DRAFT PRs, report exact SHA, done vs not done, failing/blocked tests, artifacts and rollout/rollback notes. Request detailed independent ChatGPT review. Do not merge/deploy HASHI4 or production. After explicit future approval and non-prod verification, merge/deploy in dependency-safe order (HASHI then Workbench) and verify deployed SHA, with rollback. Do not claim a green build, integration, release or production success without direct evidence.
