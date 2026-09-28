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

## Agent-owned activity Runs (accepted 2026-09-28 for HASHI1)

Cron, heartbeat, nudge, scheduler recovery, `/bg`, and background completion
callbacks are Agent activity. Their definitions, schedules, progress, and
results belong to the Agent. A Conversation Session may supply context or a
delivery surface, but it does not own the activity lifecycle.

PAO records Agent activity in one hidden `agent_activity` Session per owner and
Agent. Each occurrence is still a distinct Run with its own request identity,
attempt, provider lifecycle, and terminal state. The hidden Session is a
durable activity ledger: it is excluded from user Session lists, primary
Conversation selection, and Conversation-to-Memory promotion. A closed or
freshened Conversation therefore cannot close a scheduled Run or donate its
Engine Session accidentally.

Activity admission and presentation are separate boundaries:

1. A real activity Run publishes a concise start indication even when ordinary
   typing/progress presentation is disabled.
2. Execution success or failure is persisted independently from result
   delivery. A delivery failure never changes a failed execution into success,
   and an execution success is not proof that the user saw the report.
3. Activity receipts carry owner, Agent, task, Run, and origin references.
   Connectors may project them into the owner's current Conversation so the
   user can see what started, what ended, and ask a natural follow-up. They do
   not copy the activity prompt or raw execution history into that Conversation.
4. Completion callbacks use stable idempotency keys. Recovery may replay a
   callback safely without starting the process or activity a second time.

A user-triggered `/bg` Run receives a bounded, read-only snapshot of completed
exchanges from the origin Conversation. The reference freezes the origin
Session, context generation, and message high-water mark at admission. The Run
does not bind to that Conversation and cannot write its internal work into the
Conversation history. Scheduled activity without an explicit origin starts
with no Conversation history and uses Agent configuration plus its task
definition.

`/delay` remains a future ordinary user message in its origin Conversation.
Interactive `/loop` setup also remains in the active Conversation. These are
Conversation continuations rather than autonomous Agent activity.

The focused contract is: Agent owns the task; the activity Session owns the
ledger; each Run owns one execution; the Connector owns presentation; the
Conversation supplies only an explicit bounded context snapshot and the place
where the user sees or follows up on the result.

This HASHI1 decision is implemented in the Functions layer. Offline source and
test success remain separate from adoption by the running Worker; replacement
requires separately authorized Agent-scoped `/reboot`.
