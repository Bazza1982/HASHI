# Lulu Implementation Package: HASHI Watchtower v1

Generated: 2026-05-15T11:54:53.883583+00:00
Superloop: `sl-20260515-103210320305-ef51`
Worker: `lulu`
Workzone: `/mnt/c/Users/thene/projects/HASHI`
Windows path: `C:\Users\thene\projects\HASHI`
Target runtime: HASHI9 Windows native
Reviewer clearance: Akane plan review approved, no blockers.

## Mission

Implement or harden HASHI Watchtower v1 in the HASHI repo, targeting HASHI9 Windows native runtime. Watchtower is a minimal rescue/observability sidecar surface, not another agent runtime.

## Required v1 Scope

- `GET /control/hashi/status`
- `GET /control/hashi/logs?name=start|audit|supervisor&tail=N`
- `POST /control/hashi/start`
- Topology A only for v1:
  - Remote: Windows native
  - HASHI core: Windows native
  - launcher: `bin/bridge_ctl.ps1 -Action start -Resume`
  - fallback: `bin/bridge-u.bat --resume-last --no-pause`
  - PID semantics: Windows PID checks
  - Workbench health: HASHI9 Windows Workbench port
- Topology C (Windows Remote -> WSL2 HASHI core) is future/advanced, not v1 implementation.

## Safety Constraints

- No arbitrary shell rescue API.
- `POST /control/hashi/start` requires `L3_RESTART`.
- Default L2 Remote must return 403 for start.
- Public internet exposure is not in v1; Tailscale/internet only as documented future/security notes.
- Maintain backwards compatibility: unsupported/404 rescue endpoints must be treated as unsupported, not peer outage.

## Akane Review Constraints To Implement

- logs `tail` default 120, max 1000.
- If `tail > 1000`, truncate to 1000 and include effective tail / truncation metadata.
- Invalid/non-positive tail should return clear 400 unless existing endpoint style strongly prefers fallback.
- start `reason` persisted max 500 chars.
- reason is truncated, not rejected.
- reason is sanitized to one audit JSONL line before write.
- audit should indicate truncation when practical.
- Existing code counts only if covered by focused tests and HASHI9 smoke evidence.

## Minimum Focused Tests

Please add/update tests for:

1. status returns correct state when HASHI core is down/unreachable.
2. start under L2 returns 403 and does not start HASHI.
3. start under L3 attempts fixed Windows launcher and returns structured fields.
4. logs legal names return bounded content and effective tail metadata.
5. logs illegal names return 400 and never read arbitrary paths.
6. helper/client treats unsupported/404 rescue endpoints as unsupported.
7. audit JSONL includes requester, sanitized reason, command, pid/log path when available, outcome, status state, and error text when applicable.

## HASHI9 Windows Smoke Checklist

Final product cannot exit until Windows-side evidence covers:

1. `GET /control/hashi/status` returns correct state when HASHI core is down or unreachable.
2. `POST /control/hashi/start` under L2 config returns 403.
3. `POST /control/hashi/start` under L3 config attempts fixed Windows launcher and returns structured outcome fields.
4. `GET /control/hashi/logs` with legal names returns bounded content and effective tail metadata.
5. `GET /control/hashi/logs` with illegal name returns 400.
6. Client/helper treats unsupported/404 rescue endpoints as unsupported.
7. Audit JSONL contains required fields and sanitized/truncated reason.

## Deliverables

- Code changes in HASHI repo workzone.
- Focused tests passing.
- Operator documentation update for Topology A and future/advanced Topology C note.
- HASHI9 Windows smoke evidence summary.
- Known limitations, if any.

## Coordination

Do not request final user approval. Send implementation summary and evidence back to Zelda. Zelda will coordinate final Akane product review and evidence-based exit.
