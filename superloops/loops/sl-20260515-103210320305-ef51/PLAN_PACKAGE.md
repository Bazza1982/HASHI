# audit_vibe_coding Plan Package: HASHI Watchtower v1

Generated: 2026-05-15T11:50:05.371473+00:00
Loop: `sl-20260515-103210320305-ef51`

## Purpose

Build HASHI Watchtower v1 from idea to fully functional product using the `audit_vibe_coding` superloop pattern. Watchtower is a minimal, supervised rescue/observability sidecar for HASHI, not another agent runtime.

## Roles

- Orchestrator: `zelda`
- Worker: `lulu`
- Worker workzone: `/mnt/c/Users/thene/projects/HASHI`
- Windows path: `C:\Users\thene\projects\HASHI`
- Target runtime: `HASHI9` Windows side, not WSL
- Independent plan/final reviewer: `akane`
- Exit decider: `zelda`, evidence-based automatic exit. User is not required as final approver.

## Source Context

- `docs/HASHI_REMOTE_RESCUE_PROTOCOL.md`
- `docs/HASHI_REMOTE_SIDE_PROGRAM_UPGRADE_PLAN.md`
- User Q&A: v1 includes rescue sidecar, `/control/hashi/status`, `/control/hashi/logs`, `/control/hashi/start`, Windows-to-WSL start, and future internet/Tailscale expansion notes.

## Product Boundary

Watchtower v1 should provide a small reliable rescue surface that survives HASHI core problems and supports controlled HASHI recovery.

In scope:

- Minimal rescue sidecar / supervised remote process model.
- Fixed rescue endpoints:
  - `GET /control/hashi/status`
  - `GET /control/hashi/logs?name=start|audit|supervisor&tail=N`
  - `POST /control/hashi/start`
- Windows-side operation on HASHI9.
- Windows-to-WSL start handling where relevant.
- Structured audit logs for rescue actions.
- Capability advertisement and safe unsupported behavior for older peers.
- Future internet/Tailscale notes, without making v1 a public admin surface.

Non-goals:

- No generic arbitrary shell API for rescue v1.
- No public internet exposure by default.
- No new agent runtime inside Watchtower.
- No requirement that every peer upgrade simultaneously.
- No removal of existing `/remote on` convenience behavior.

## Architecture Constraints

- Keep core minimal and modular.
- Rescue/start is restart-class control and must remain gated (`L3_RESTART`).
- Preserve backwards/forwards compatibility with older Remotes.
- Logging must be comprehensive enough for audit and debugging.
- Features should be plug-and-play; avoid monolithic core changes.
- HASHI9 Windows is the target environment for worker implementation and final smoke test.

## Proposed Execution Flow

1. Confirm intake decisions and evidence-based exit policy.
2. Extract requirements from existing rescue/side-program docs.
3. Prepare implementation plan and review package.
4. Send plan to Akane for independent review.
5. Address all actionable plan-review comments.
6. Hand off implementation package to Lulu in the HASHI9 workzone.
7. Lulu implements the approved slice in `C:\Users\thene\projects\HASHI` via workzone.
8. Run focused automated tests.
9. Run HASHI9 Windows-side smoke test.
10. Run safe live-test preflight before any destructive or real-world action:
    - name protected systems that must not be stopped or modified
    - identify exact target processes/services and allowed failure actions
    - prove the control channel is independent of the component being stopped
    - confirm shared-token/auth level, dynamic port, and external route expectations
    - define rollback/restore steps before starting the live test
11. Run real live acceptance through the intended external/user path, not a local bypass:
    - use the deployed sidecar/runtime and LAN/WSL/Windows route the product promises to support
    - perform controlled off/on, failure/recovery, restart, persistence, or visibility checks when relevant
    - verify status/list views, health endpoints, audit logs, process state, and user-visible behavior from all required directions
12. If live acceptance fails, restore service first, record evidence, debug root cause, patch, rerun focused automated tests, then repeat live acceptance until green.
13. Collect evidence: logs, test output, live acceptance output, list/status views, docs, known limitations.
14. Send final product/evidence to Akane for final review.
15. Address all actionable final-review comments with fix/retest loops, including repeated live acceptance when review comments touch real runtime behavior.
16. Zelda exits automatically only when criteria are satisfied, live acceptance is green or explicitly impossible with accepted rationale, and no blocking Akane comments remain.

## Acceptance Criteria

- Worker assignment to Lulu is confirmed and workzone is active.
- Plan review by Akane is completed before implementation starts.
- All actionable plan-review comments are addressed or explicitly accepted.
- Watchtower v1 scope is implemented and covered by focused tests; existing code cannot satisfy acceptance without test coverage and real smoke evidence.
- Focused automated tests pass.
- HASHI9 Windows-side real smoke test passes.
- Safe live-test preflight is recorded before any destructive test: protected systems, target components, independent control channel, auth, route/port expectations, and rollback plan.
- Real live acceptance passes through the intended external/user path, not a local shortcut or mock-only path.
- Products that promise rescue, restart, persistence, network visibility, or runtime supervision must be tested with controlled live off/on or failure/recovery behavior.
- Live visibility evidence exists from every required direction/view: status endpoints, list views, LAN/WSL/Windows perspectives as applicable, health checks, process state, and audit logs.
- Any live failure triggers restore -> evidence -> debug -> fix -> automated retest -> live retest until green; the superloop cannot exit on smoke tests alone.
- Structured audit evidence exists for rescue actions.
- Operator docs explain supported endpoints, safety gate, and Windows/WSL handling.
- Final product review by Akane is completed before exit.
- All actionable final review comments are fixed and retested.
- No known blocker remains for normal operation.

## Requested Review From Akane

Please review this plan before implementation begins. Focus on:

1. Missing v1 scope or hidden assumptions.
2. Whether worker/workzone topology is coherent.
3. Whether the rescue API is narrow and safe enough.
4. Whether Windows/WSL handling is sufficiently explicit.
5. Whether acceptance criteria are strong enough to allow automatic exit.
6. Any blocker that must be addressed before Lulu starts implementation.

## Plan Review Response v2

Reviewer: `akane`
Review date: 2026-05-15
Status: addressed by Zelda before implementation handoff.

### B1 Resolution: HASHI9 Topology

Selected topology for Watchtower v1 implementation and smoke testing is **Topology A**:

| Component | Location |
|---|---|
| Remote process | HASHI9 Windows native |
| HASHI core process | HASHI9 Windows native |
| Repo path | `C:\Users\thene\projects\HASHI` |
| Lulu workzone from HASHI1/WSL | `/mnt/c/Users/thene/projects/HASHI` |
| Start mechanism | Windows native launcher: `bin/bridge_ctl.ps1 -Action start -Resume`, fallback `bin/bridge-u.bat --resume-last --no-pause` |
| PID semantics | Windows PID / Windows-side process checks |
| Workbench health | Windows localhost / configured HASHI9 Workbench port |

This v1 does **not** implement Topology C as the primary path. Windows-native Remote starting WSL2 HASHI core is a documented future/advanced topology and must not be silently assumed. If future work adds Topology C, it needs explicit WSL PID/file/health handling using `wsl.exe` or a dedicated cross-boundary adapter.

### B2 Resolution: Logs Endpoint Scope

Watchtower v1 **includes**:

```text
GET /control/hashi/logs?name=start|audit|supervisor&tail=N
```

The endpoint is part of the v1 acceptance surface, not future scope.

### M1 Resolution: Logs Tail Bound

`tail` must be bounded:

- default: 120 lines
- maximum: 1000 lines
- if requested `tail > 1000`, truncate to 1000 and include response metadata indicating truncation/effective_tail.
- invalid/non-positive values should fall back to default or return a clear 400, depending on existing endpoint style.

### M2 Resolution: Start Reason Bound

`POST /control/hashi/start` reason handling:

- maximum persisted reason length: 500 characters
- longer input is truncated, not rejected
- sanitize to one line before audit JSONL write: replace CR/LF/control separators with spaces
- audit record should indicate if truncation occurred when practical

### L1 Resolution: Acceptance Criteria Tightening

The previous acceptance escape hatch is removed. Existing endpoints or code paths only count if they are covered by focused tests and smoke evidence.

### L2 Resolution: Minimum HASHI9 Windows Smoke Checklist

Minimum Windows-side smoke test coverage:

1. `GET /control/hashi/status` returns the correct state when HASHI core is down or unreachable.
2. `POST /control/hashi/start` under L2 config returns 403 and does not start HASHI.
3. `POST /control/hashi/start` under L3 config attempts the fixed Windows launcher and returns structured `started` / `command` / `log_path` / outcome fields.
4. `GET /control/hashi/logs` with legal names returns bounded content and effective tail metadata.
5. `GET /control/hashi/logs` with illegal name returns 400 and does not read arbitrary paths.
6. Client/helper treats unsupported/404 rescue endpoints as `unsupported`, not peer outage.
7. Audit JSONL contains requester, sanitized reason, command, PID/log path when available, outcome, status state, and error text when applicable.

### L3 Resolution: Operator Docs Dependency

Operator docs must describe Topology A as the v1 supported target, and explicitly label Windows-native Remote -> WSL2 HASHI core as future/advanced unless implemented and tested separately.

## Implementation Handoff Constraints For Lulu

Lulu may start implementation only after Akane confirms no blocking plan-review comments remain or Zelda records that B1/B2 are resolved and no additional blocker was raised.

Implementation must preserve:

- narrow fixed rescue API
- L3 start gate
- bounded logs endpoint
- sanitized/truncated reason audit field
- focused tests before real smoke
- HASHI9 Windows-side final validation
