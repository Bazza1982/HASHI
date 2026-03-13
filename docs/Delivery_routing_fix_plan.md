# 2026-03-11 Delivery Routing Fix Plan

## Scope

This plan covers the bugs currently worth fixing from the 2026-03-11 debugging pass:

1. Priority 1: automatic outbound replies failing with `telegram.error.BadRequest: Chat not found`
2. Priority 2: transient Telegram transport failures (`httpx.ConnectError`, `httpx.ReadError`) after the routing fix is stable

It does not reopen items that already appear fixed in code:
- admin API JSON serialization
- OpenRouter closed-client lifecycle
- Codex stdin/chunk-limit hardening

## Current Evidence

`docs/DEBUGGING.md` shows the `Chat not found` failures cluster on automatic send paths (`source=system`, `source=fyi`) rather than normal interactive API prompts.

The current implementation still hardwires `global.authorized_id` into non-interactive enqueue/send branches:

- `orchestrator/agent_runtime.py`
  - scheduler skill action reply
  - scheduler prompt enqueue
  - manual job run reply
  - manual job run prompt enqueue
- `orchestrator/flexible_agent_runtime.py`
  - scheduler skill prompt enqueue
  - scheduler skill action reply
  - manual job run prompt enqueue/reply branches

This matches the failure pattern: successful backend work can still be dropped if the runtime chooses the wrong outbound target.

## Fix Strategy

### Phase 1: Fix chat-target resolution

Goal:
Make every non-interactive branch send to a resolved, known-good chat target instead of blindly using `global.authorized_id`.

Implementation steps:

1. Introduce a single chat-target resolution helper in both runtimes.
   - Suggested shape: `resolve_default_chat_id()` or `resolve_delivery_chat_id(source: str | None = None)`.
   - Return order should prefer:
     - last known successful Telegram chat for this agent/session
     - configured explicit default chat if one exists
     - legacy `global.authorized_id` only as a final fallback

2. Track/update the last known good chat id from real Telegram user interactions.
   - Update it when receiving authorized inbound user messages/callbacks.
   - Optionally update it after a successful outbound send if the request already had a valid `chat_id`.

3. Replace hardcoded `global.authorized_id` usage in automatic branches.
   - Scheduler enqueue paths
   - FYI/system-triggered delivery paths
   - Manual skill/job execution notifications
   - Any other non-interactive branch that constructs a synthetic request without an explicit chat target

4. Fail closed when no valid target can be resolved.
   - Do not attempt Telegram send with a guessed/bad target.
   - Log a structured error such as `No delivery chat resolved for agent <name> source=<source>`.
   - Keep backend completion status separate from delivery failure status.

5. Keep the change minimal.
   - Do not redesign the request model unless needed.
   - First patch should centralize resolution and swap call sites only.

### Phase 2: Strengthen telemetry for routing bugs

Goal:
Make future routing regressions obvious in logs and stress tests.

Implementation steps:

1. Log delivery target provenance.
   - For each automatic enqueue/send, log which source produced the chat id:
     - `last_known_chat`
     - `config_default`
     - `authorized_id_fallback`
     - `unresolved`

2. Add branch labels to errors.
   - Include agent, source, request id, and resolved chat id in delivery exceptions.

3. Update stress-test reporting.
   - Split delivery failures by source (`api`, `system`, `fyi`, `scheduler`, `scheduler-skill`).
   - Report backend success separately from send success.

### Phase 3: Telegram transport hardening

Goal:
Reduce noise and avoid losing replies from transient network faults after target routing is correct.

Implementation steps:

1. Wrap outbound Telegram sends with small capped retries for transient exceptions only.
   - Retry candidates: `httpx.ConnectError`, `httpx.ReadError`, timeout-class failures
   - Non-retry candidates: `telegram.error.BadRequest` and other deterministic request errors

2. Apply retries consistently to:
   - normal text sends
   - chunked long-message sends
   - placeholder sends/deletes where safe
   - voice sends if they are part of the active workflow

3. Preserve first-failure evidence in logs.
   - Log the initial exception before retrying.
   - Log final outcome with attempt count.

4. Keep retry policy narrow.
   - Small attempt count
   - short backoff
   - no infinite loops

## Suggested Order Of Work

1. Patch `agent_runtime.py` target resolution
2. Patch `flexible_agent_runtime.py` the same way
3. Add targeted logging around resolved delivery target
4. Run branch-specific validation for `api`, `system`, `fyi`, and scheduler paths
5. Only then add transient transport retries

## Validation Plan

### Manual validation

1. Trigger a normal API prompt to confirm baseline replies still arrive.
2. Trigger an FYI delivery and confirm it reaches the same valid chat.
3. Trigger a system-generated message and confirm it reaches the same valid chat.
4. Trigger a scheduler or skill-run notification and confirm it reaches the same valid chat.
5. Verify no branch still logs `Chat not found`.

### Log validation

Check:
- per-agent `events.log`
- per-agent `errors.log`
- any delivery log lines now include source + resolved target provenance

Expected result:
- backend success with matching delivery success on automatic branches
- if no target exists, explicit `unresolved` logging instead of Telegram `BadRequest`

### Regression guardrails

Add or extend tests for:
- helper returns last known chat when available
- helper falls back in the intended order
- automatic branches call the helper instead of using `global.authorized_id` directly
- transient retry wrapper does not retry `BadRequest`

## Risks

1. If `authorized_id` has historically been used as both user-id and chat-id, some branches may currently work only by accident.
2. If multiple chats are valid for the same authorized user, a single cached default chat may still be too coarse.
3. Flex runtime and fixed runtime can drift if patched independently; the helper logic should stay behaviorally identical.

## Success Criteria

- No `telegram.error.BadRequest: Chat not found` on automatic send branches during the next soak run
- Automatic replies for `system`, `fyi`, scheduler, and skill-job branches reach the intended chat
- Transient transport failures are visibly separated from deterministic routing bugs
- Existing interactive API behavior remains unchanged
