# Agent activity visibility

Accepted 2026-09-07 for HASHI2 and HASHI3. Owner: PAO request admission and
Frontend Connectors, Functions layer.

Every admitted Agent turn requests user-visible delivery. API, HChat messages
and replies, bridge tasks, scheduler turns and continuation turns follow the
same policy. `deliver_to_telegram` is always true at queue admission; legacy
false values are normalized, not honored. `silent` cannot hide an Agent turn.
Compatibility wire fields remain accepted so old clients can finish their work.
The common enforcement point is `FlexibleAgentRuntime.enqueue_request`; do not
add a source-specific suppression exception or move this policy into Core.

Delivery retains the existing destination and Session routing. This policy does
not grant permission to contact another user, change credentials or recursively
reply to HChat terminal messages. Existing presentation preferences control the
level of detail; delivery failures remain failures, not proof of receipt.

API and bridge callers explicitly request delivery. Older protected scheduler
and IPC implementations can still supply false; Function admission overrides
it. No protected Core changes are required on either instance.

Source and offline tests do not prove live adoption. Existing immutable Workers
continue using their loaded generation until an authorized replacement. No
production restart is part of this change.
