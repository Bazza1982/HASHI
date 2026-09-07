# HASHI2 minimal Core migration — 2026-09-07

## Approval and scope

The user explicitly authorized minimizing Core and moving all replaceable
behavior into Functions on HASHI2. The operator will perform the cold restart.
No production reboot, restart, gateway request or Agent message is part of this
implementation. Work is isolated on `refactor/minimal-core-20260907`.

Checkpoint before extraction: `25f7ef1e`. It preserves the earlier reviewed
backend/defaults, UI/FYI and lightweight safeguard work. Instance-local Agent
configuration, credentials, state and backups are outside this change.

## Concrete migration

The former shared coordinator moves from `main.py` to the immutable shared
Functions process. Model/effort/catalogue data, activity presentation, terminal
and UI language, gateway execution, scheduler/HER job policy and shared Backend
API now share a real replaceable process lifetime. Core imports none of them.
The original per-Agent replacement transaction remains intact inside Functions.

A separate explicit shared replacement entry prepares candidates and drains
work before handing over all shared services. It preserves the supervising
Core/lock and durable stores, but has a documented shared-service gap. This is
not a promise of interruption-free updates or a mere manifest deletion.

Protected source changes from 69 files / 1,162,603 bytes at the checkpoint to
9 files / 74,721 bytes. The reduction reflects process ownership, not a renamed
protection list. The retained guard prevents import-time candidate side effects;
OS process groups/Jobs prevent descendants surviving their supervising process.

## Independent review and corrections

A separate Codex read-only review examined the baseline diff and all new source.
It confirmed substantive separation and reported seven regressions. The owner
corrected them before integration:

| Finding | Correction and evidence |
|---|---|
| Core/Functions canonical home mismatch | One canonical home helper, passed unchanged to children; environment/quote/literal-path checks |
| Configuration installation root became an artifact path | Explicit source root in shared and Agent configuration; actual spawned product preparation checks source/home identity |
| Rollback downgraded independently updated Agents | Per-Agent artifact/receipt snapshots and exact restore; mixed-generation artifact verification |
| Shared crash reset selected Agents and discarded pending messages | Durable installed topology and acceptance offsets; recovery retains the set and never drops pending Telegram updates |
| Cancelled drain stranded a route gate | Cancellation-safe gate acquisition; reproduced failing test passes after fix |
| Transient connector failure prevented further activation | Independent activation, degraded health and bounded-delay retries; healthy connectors continue |
| First-run onboarding disappeared | Initial-only interactive preparation; empty replacement configuration is rejected before draining |

The review also identified Windows descendant cleanup as a platform risk.
OS Job ownership was added; native Windows execution remains unverified here.
No second independent review or production acceptance is claimed.

## Verification record

Red evidence: the new ownership/import checks fail on the previous source:
`flexible_backend_registry` is protected and `startup_manager` lazily imports
functional rendering into the long-lived process. Both pass after extraction.

Real isolated subprocess tests exercise code adoption, rejected preparation,
artifact tampering, activation rollback, busy drain, unchanged Core PID and
persistent progress. The actual full product closure also prepares in a spawned
process using temporary configuration without starting live services.

Additional reversible mutations removed the first-import guard, introduced a
lazy `from orchestrator import config` in Core, and discarded persisted Telegram
acceptance offsets. Each focused check failed; all mutations were restored.

- Focused process/import/handoff/ingress/HTTP checks: 47 passed.
- Final curated Core gate after review corrections: 585 passed.
- Python compilation: 36 changed/new modules; Ruff passed.
- Protected-Core manifest/preflight: passed with this task's explicit authorization.
- Final expanded offline suite after review corrections: 3,740 passed,
  158 explicitly excluded by scope, one third-party AnyIO deprecation warning.
- Final artifact/import/FYI checks after comment/whitespace cleanup: 17 passed.

No live adoption is claimed. Native Windows and real provider/network behavior
are not established by these offline checks.

## Operator cold adoption

The existing production Core remained PID 184637 during implementation. No
reboot/restart or shared replacement command was executed. Six configured
Agents retain Codex CLI, GPT-6 Astra, medium effort and Fixed defaults. Their
local configuration/state/backup files are not part of the source commit.

After the operator's cold start, verify separate Core/shared/Agent process
identities and installed generation metadata, then confirm the effective
Engine/model/effort/mode in the intended Agents. A successful source test or
queued replacement request is not evidence of live adoption. Shared-service
replacement still has a service gap; Agent-only replacement keeps its scope.
