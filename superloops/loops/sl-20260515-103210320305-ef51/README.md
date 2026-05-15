# Superloop from slrec-20260515-103210314239-bdf6

- loop_id: `sl-20260515-103210320305-ef51`
- recording_id: `slrec-20260515-103210314239-bdf6`
- status: `paused`
- owner: `zelda@HASHI1`

## Snapshot

- tasks: 10
- waits: 0
- issues: 0

## Independent Review Gates

- Default reviewer: `akane` via `/hchat akane`
- Plan review gate: after planning/evidence checkpoint design, before implementation starts.
- Final product review gate: after implementation, tests, smoke evidence, and docs, before exit.
- Rule: every actionable review comment must become a task or issue and must be addressed, explicitly accepted, or retested before proceeding.
- Rationale: cross-factory model review catches different failure modes than the orchestrator.

## Intake Q&A Decisions

- Orchestrator: `zelda`
- Worker: `lulu`
- Independent reviewer: `akane`
- Target development/runtime: `HASHI9` Windows side, not WSL.
- Codebase: HASHI repo.
- Watchtower v1 scope: rescue sidecar, `/control/hashi/status`, `/control/hashi/logs`, `/control/hashi/start`, Windows-to-WSL start, and future internet/Tailscale expansion notes.
- Exit authority: Zelda exits automatically only when objective evidence satisfies all exit criteria and Akane has no blocking review comments.
- Open topology risk: `lulu` currently routes locally on HASHI1; `lulu@HASHI9` has no active route.

## Worker Workzone

- Worker: `lulu`
- Workzone source: `superloop:audit_vibe_coding`
- Workzone path visible from HASHI1/WSL: `/mnt/c/Users/thene/projects/HASHI`
- Windows path: `C:\Users\thene\projects\HASHI`
- Target instance/runtime: `HASHI9` Windows side.
- Rule: lulu may work through the workzone path, but completion requires tests and smoke validation on HASHI9 Windows.

## Safe Live Acceptance Gate

This superloop must not exit on implementation, unit tests, debugging, smoke tests, or final review alone when the product has real runtime behavior. It must include a safe live acceptance loop.

- Preflight first: identify protected systems, allowed target components, exact stop/failure action, independent rescue/control channel, auth level, dynamic port/route expectations, and rollback steps.
- Live test through the promised path: use the deployed product as an external/user peer would, not a local shortcut or mocked helper.
- Verify from all required views: health/status endpoints, `/remote list`-style visibility, LAN/WSL/Windows perspectives when applicable, audit logs, process state, and user-visible behavior.
- Loop on failure: restore service first, record evidence, debug, patch, rerun focused automated tests, rerun live acceptance, and repeat until green.
- Exit rule: Zelda may exit only when live acceptance evidence proves the product works in real life, or when live testing is impossible and the limitation is explicitly documented and accepted.
