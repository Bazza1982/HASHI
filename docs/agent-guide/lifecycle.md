# Agent and instance lifecycle

## Purpose and when to read
Read for Agent creation, start, stop, deletion, reboot, Move, Clone, jobs and instance update work.

## Ownership and layer
PAO owns Agent identity, lifecycle, outer jobs and operation receipts in shared/per-Agent Functions. Core supplies process primitives without product policy.

## Distinctions
Configured, active, running, stopped, offline and inactive differ. Move confirmation is forward-only; Clone does not inherit Move-only source mutations. Completion, delivery, runtime adoption and physical acceptance are independent.

## Authoritative documents
- [PAO lifecycle](../HASHI_PAO_SYSTEM_DESIGN.md#21-agent-and-runtime-control).
- [Agent Move and Clone](../HASHI_AGENT_MOVE_V1.md).
- [Stop fencing](../HASHI_AGENT_STOP_FENCE_DECISION.md).
- [Instance CLI lifecycle](../HASHI_INSTANCE_CLI_LIFECYCLE.md).
- [Reboot receipts](../HASHI_REBOOT_RECEIPTS.md) and [background jobs](../HASHI_BACKGROUND_JOBS_DESIGN.md).

## Source and verification
Start with `orchestrator/agent_move/service.py`, `orchestrator/agent_deletion.py` and `orchestrator/reboot_manager.py`. Select `tests/test_agent_move_service.py`, `tests/test_agent_deletion.py` or `tests/test_reboot_manager.py` according to the changed contract. Verify rollback/forward-only boundaries, matching identities, partial cleanup and durable receipts rather than only an HTTP acknowledgement.

## Adoption
Read the versioned live acceptance contract before any authorized real lifecycle test. A source fix does not restore an already damaged instance automatically. [Standby implementation planning](../HASHI_STANDBY_IMPLEMENTATION_PLAN.md) contains qualification stages, not permission to ARM or a claim of current integration.
