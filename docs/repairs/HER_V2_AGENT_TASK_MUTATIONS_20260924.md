# HER v2 Agent-owned task mutations — 2026-09-24

## Scope

- Instance: `HASHI4`
- Branch: `feat/jev-skill`
- Functional owner: HER v2 + PAO Scheduler/Superloop Functions
- Core status: unchanged

## Approval

The current user explicitly requested that HER v2 Agents be able to create,
manage, and delete their own recurring and long-running tasks, including the
work behind `/loop`, `/nudge`, and `/superloop`.

## Implementation

- Added typed Agent-scoped Scheduler create/update/delete operations for Cron,
  Heartbeat, and Nudge jobs.
- Added typed Agent-scoped Superloop list/get/create/update/delete operations.
- Bound every mutation to the Agent identity selected by the Workbench API;
  foreign tasks are not addressable, and deletion requires explicit
  authorization.
- Replaced `/loop`'s direct `tasks.json` editing instructions with the typed
  Scheduler create tool.
- Kept direct file editing out of the HER v2 contract and preserved existing
  Scheduler/Superloop state writers.

## Verification

Focused tests cover persistence, ownership rejection, deletion authorization,
gateway routing, Nudge interval/exit updates, and Superloop lifecycle. The
source-level protected-Core check passed. No running Worker or Function was
restarted in this change, so live adoption remains unverified until the normal
operational rollout.
