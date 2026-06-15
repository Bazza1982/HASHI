# Telegram Delivery Failover And Preview Control Plan

## Problem

When an agent's Telegram bot hits flood control, HASHI currently treats the
send failure as a local delivery miss instead of an explicit operational
incident.

In the Kasumi case:

- Grok completed the request successfully.
- Telegram returned `RetryAfter`.
- answer preview edits had already been active during a long run;
- final delivery still tried to use the same blocked token;
- the user saw silence or fragmented failures instead of one clear warning.

This creates four concrete problems:

1. successful answers can be lost at delivery time;
2. preview/thinking/final sends may keep hitting the blocked token;
3. the user does not know whether the model failed or Telegram failed;
4. long-task agents like `kasumi` have no Telegram command to disable answer
   stream preview quickly.

## Goal

Implement one instance-local Telegram delivery health system that:

1. detects the first `RetryAfter` on a source agent;
2. records one delivery incident;
3. suppresses further Telegram sends from the blocked source token;
4. saves any undelivered successful response;
5. sends one operational warning through a same-instance failover agent;
6. rate-limits repeated warnings while the block is active;
7. automatically recovers after `blocked_until`;
8. sends one recovery notice and returns future delivery to the original agent;
9. adds a Telegram `/preview` command so operators can disable answer stream
   preview per agent without editing config files.

For HASHI2, the default failover agent is:

```text
lin_yueru
```

## Non-Goals

- Do not solve every Telegram transport failure class in the first pass.
- Do not introduce a new external notification service.
- Do not auto-restart blocked agents.
- Do not transfer conversation ownership from the source agent to the failover
  agent.
- Do not replay undelivered answers automatically in v1.

## First-Class Principles

### 1. One Delivery Gate

All Telegram user-visible sends must pass through one shared delivery gate.
This includes:

- `send_long_message()`
- `_reply_text()`
- `_send_text()`
- answer preview placeholder edits
- answer stream final promotion edits
- recovery notices
- failover notices

No Telegram path should handle `RetryAfter` privately once this lands.

### 2. One Incident Per Block Window

The first `RetryAfter` opens one active incident for the blocked source token.
Further blocked sends extend or update that incident instead of creating new
warning storms.

### 3. Failover Is Notification-Only

Failover delivery is only for short operational notices such as:

- delivery blocked
- still blocked reminder
- delivery recovered

The actual answer remains attributed to the original source agent and is saved
for manual recovery or later replay tooling.

### 4. Preview Is Optional And Operator-Controlled

Answer preview is useful for short interactive runs but harmful for long
Grok-style tasks that may accumulate many Telegram edits before final delivery.

HASHI must support:

- `/preview on`
- `/preview off`
- `/preview status`

per agent, with persisted state.

## Configuration

Add optional global config in `agents.json`:

```json
{
  "global": {
    "telegram_delivery_failover": {
      "enabled": true,
      "default_agent": "lin_yueru",
      "warning_reminder_seconds": 600,
      "watcher_poll_seconds": 60
    }
  }
}
```

Add optional per-agent config in `extra`:

```json
{
  "extra": {
    "answer_stream_preview": true
  }
}
```

Interpretation:

- `answer_stream_preview` remains the config default;
- runtime state may override it through `/preview on|off`;
- long-task Grok agents like `kasumi` should default to `false`.

## Runtime State

Add:

```text
state/telegram_delivery_health.json
```

Suggested shape:

```json
{
  "version": 1,
  "agents": {
    "kasumi": {
      "token_key": "telegram:kasumi",
      "status": "blocked",
      "blocked_until": "2026-06-15T20:39:07+10:00",
      "retry_after_s": 30029,
      "incident_id": "tg-kasumi-20260615T121838",
      "last_incident_at": "2026-06-15T12:18:38+10:00",
      "last_request_id": "req-0001",
      "active_failover_agent": "lin_yueru",
      "failover_failed": false,
      "recovery_notice_sent_at": null,
      "per_chat": {
        "123456789": {
          "first_warned_at": "2026-06-15T12:18:39+10:00",
          "last_warned_at": "2026-06-15T12:18:39+10:00",
          "last_warning_request_id": "req-0001",
          "recovery_notice_sent_at": null,
          "undelivered_request_ids": [
            "req-0001"
          ]
        }
      }
    }
  }
}
```

Add persisted preview state:

```text
state/runtime_preferences.json
```

Suggested shape:

```json
{
  "version": 1,
  "answer_stream_preview": false
}
```

Add undelivered response storage:

```text
workspaces/<agent>/undelivered/<request_id>.json
workspaces/<agent>/undelivered/<request_id>.md
```

The JSON sidecar should include at least:

- `request_id`
- `chat_id`
- `source_agent`
- `incident_id`
- `delivery_purpose`
- `backend_completed_at`
- `retry_after_s`
- `blocked_until`
- `failover_agent`
- `content_sha256`
- `markdown_path`

## Incident State Machine

Per source agent or token, use the following states:

- `healthy`
- `blocked`
- `recovery_due`
- `recovered`

Behavior:

1. First `RetryAfter` opens `blocked`.
2. Watcher moves `blocked -> recovery_due` when `now >= blocked_until`.
3. Recovery notice success moves `recovery_due -> recovered`, then immediately
   clears active failover routing and returns to `healthy`.
4. Recovery notice `RetryAfter` moves back to `blocked` with a new
   `blocked_until`.

The important rule is:

**routing returns to the original agent only after the recovery transition is
completed, not merely because wall-clock time has passed.**

## Preview Preference Behavior

Add Telegram command:

```text
/preview on
/preview off
/preview status
```

Command behavior:

- `on`: persist `answer_stream_preview=true` in runtime preferences
- `off`: persist `answer_stream_preview=false`
- `status`: report effective preview state, config default, and persisted
  override if any

Effective preview decision order:

1. persisted runtime preference from `state/runtime_preferences.json`
2. `config.extra.answer_stream_preview`
3. default `true`

Status should show:

- preview `ON` or `OFF`
- whether the value came from persisted override or config default
- recommendation text for long-task Grok agents if preview is `ON`

Recommendation:

- set `/preview off` by default for long-task Grok agents like `kasumi`
- leave preview available for short-response conversational agents

## Runtime Behavior

### 1. Source Agent Hits `RetryAfter`

When any Telegram send path catches `RetryAfter`:

1. normalize the exception into a shared delivery incident path;
2. compute `blocked_until = now + retry_after_s`;
3. mark the source agent as blocked;
4. suppress further Telegram sends by the blocked source token;
5. persist undelivered response content when available;
6. send one failover warning if allowed by dedupe rules.

The blocked source agent may still:

- receive the request;
- execute the backend call;
- produce a successful final answer;
- persist that answer locally.

The blocked source agent must not keep attempting preview/final/error sends
through the blocked token during the active block window.

### 2. Unified Delivery Gate Rules

Before any Telegram send:

1. determine `source_agent`;
2. determine `chat_id`;
3. determine `delivery_mode`.

Supported `delivery_mode` values:

- `normal_reply`
- `normal_send`
- `preview_edit`
- `final_delivery`
- `recovery_notice`
- `failover_notice`

Gate rules:

- if source agent is not blocked: attempt send normally;
- if source agent is blocked and mode is not `recovery_notice`: suppress the
  source send;
- if source agent is blocked and mode is `failover_notice`: allow only the
  selected failover agent to send;
- `failover_notice` is never allowed to recurse into another failover chain.

### 3. Failover Agent Selection

Selection order:

1. configured `global.telegram_delivery_failover.default_agent`
2. first running runtime with:
   - `name != source_agent`
   - `telegram_connected == true`
   - real Telegram token, not `WORKBENCH_ONLY_NO_TOKEN`
   - not currently delivery-blocked
3. if none is available, record the incident only

Selection hard rules:

- do not select the source agent;
- do not select an already blocked candidate;
- do not rotate candidates repeatedly inside one active incident;
- if the chosen failover agent later fails, try at most one alternate candidate;
- if both fail, stop and rely on Workbench/state/log visibility.

### 4. Failover Message

The failover agent sends one short operational warning:

```text
Delivery warning from HASHI2:

kasumi generated a response, but her Telegram bot is flood-limited.

Request: req-0001
Retry after: 30029s
Blocked until: 2026-06-15T20:39:07+10:00
Saved response: workspaces/kasumi/undelivered/req-0001.md

Please use lin_yueru or Workbench until kasumi's Telegram delivery recovers.
```

Wording requirements:

- clearly say backend generation succeeded if it did;
- clearly say the problem is Telegram delivery, not model failure;
- clearly say the failover agent is only notifying, not taking over the agent.

### 5. Warning Dedupe And Rate Limiting

Per active incident and per chat:

- one immediate failover warning on the first blocked send;
- one reminder at most every `warning_reminder_seconds`, default 600s;
- always update incident state and logs even if no user-visible warning is sent.

This dedupe must be keyed by both:

- `source_agent`
- `chat_id`

Do not dedupe only by source agent, or one chat may suppress another chat's
warning incorrectly.

### 6. Preview Behavior While Blocked

If the source agent is blocked:

- no answer preview edits should be attempted;
- no answer stream final promotion edit should be attempted;
- no placeholder continuation chunks should be attempted through the blocked
  token;
- if final text exists, save it as undelivered.

Additionally:

- if preview is disabled by `/preview off`, the runtime should bypass preview
  setup entirely for that agent;
- `/verbose off` and `/think off` remain separate controls and do not replace
  `/preview off`.

### 7. Recovery

Watcher behavior:

1. poll every `watcher_poll_seconds`, default 60s;
2. for each blocked source agent, if `now >= blocked_until`, mark
   `recovery_due`;
3. send one recovery notice;
4. on success, clear blocked state and stop failover routing;
5. on `RetryAfter`, extend the existing incident and return to `blocked`.

Recovery notice:

```text
Delivery recovered:

kasumi's Telegram delivery block has expired.
You can continue using kasumi normally.
```

Recovery routing rules:

- prefer the recovered source agent's own token;
- if recovery send fails with `RetryAfter`, do not falsely claim recovery;
- only after recovery notice success does routing return to the original agent.

## Code Landing Points

### Primary Files

- `orchestrator/runtime_delivery.py`
  - make this the shared delivery gate entry for long-form final sends
  - normalize `RetryAfter` into one incident API

- `orchestrator/flexible_agent_runtime.py`
  - route `_reply_text()` and `_send_text()` through the same delivery gate
  - expose preview preference helpers
  - expose delivery-blocked metadata for status

- `orchestrator/runtime_pipeline.py`
  - read effective preview preference instead of raw config only
  - suppress preview edits when delivery is blocked
  - persist final successful response before or at blocked delivery failure
  - suppress final promotion retry loops once blocked

- `orchestrator/service_manager.py`
  - start one delivery-health watcher per instance
  - stop the watcher on shutdown

- `orchestrator/runtime_status.py`
  - add delivery-blocked and preview state to `/status`

### New Helper Module

- `orchestrator/telegram_delivery_failover.py`
  - load/save delivery health state
  - incident creation/update
  - blocked-until calculation
  - failover agent selection
  - per-chat dedupe
  - undelivered response persistence
  - recovery watcher logic
  - shared helpers for `is_blocked()`, `should_warn()`, `mark_recovered()`

### New Command Surface

- `orchestrator/runtime_preview.py`
  - implement `/preview on|off|status`
  - persist preview preference
  - show effective state and recommendation text

If a new module is undesirable, the command may also live in an existing
runtime command module, but the implementation must remain separate from the
delivery failover logic.

## Status And Observability

`/status` should show, at minimum:

- `Telegram delivery: healthy` or `blocked until ...`
- active failover agent if blocked
- preview `ON/OFF`
- preview source: `persisted override` or `config default`

Recommended detail text:

```text
📨 Delivery: blocked until 20:39 via lin_yueru
👁 Preview: OFF (persisted override)
```

Workbench or job surfaces should also be able to indicate:

- backend success but delivery blocked
- undelivered response saved
- incident id and blocked-until time

## Failure Handling Rules

### Failover Agent Also Hits `RetryAfter`

1. record `failover_failed`;
2. do not recurse failover from a `failover_notice`;
3. try at most one alternate candidate;
4. if both fail, stop there.

### Recovery Notice Hits `RetryAfter`

1. treat it as the same incident extending or reopening the block;
2. recompute `blocked_until`;
3. leave failover routing active;
4. send at most one new warning per dedupe window.

### Persisted Response Save Fails

1. record `undelivered_persist_failed`;
2. still keep the delivery incident;
3. still attempt one failover warning if possible;
4. include in logs that backend output existed but local save failed.

## Edge Cases To Cover

1. The source agent and another agent share the same Telegram token.
   In that case, block by token identity, not just by agent name.
2. Preview placeholder was created successfully, but later preview edits hit
   `RetryAfter`.
3. Final answer exists, but continuation chunk sending fails after stream final
   promotion partially succeeded.
4. Recovery notice succeeds, then the next normal message immediately hits a new
   `RetryAfter`.
5. User sends multiple messages during a long block; only reminders should be
   emitted, not one warning per request.
6. Failover agent is online but Workbench-only with no real Telegram token.
7. Block expires while the source agent process restarts; watcher must resume
   from persisted state.
8. Preview is toggled off while a request is already running; only future
   preview activity should be guaranteed, not retroactive edit cancellation.
9. `/preview status` must still work when Telegram delivery is blocked, if sent
   through an unblocked local command path or Workbench; if not possible via the
   blocked source token, it should at least be visible in `/status`.

## Acceptance Criteria

1. First source-agent `RetryAfter` writes
   `state/telegram_delivery_health.json`.
2. A successful undelivered answer is saved under
   `workspaces/<agent>/undelivered/<request_id>.md` and sidecar JSON.
3. The source agent stops using the blocked token for preview/thinking/final
   sends during the active block window.
4. The selected failover agent sends one warning if its token is available.
5. Repeated blocked requests do not spam; reminders are deduped per
   source-agent/chat.
6. `failover_notice` never recursively triggers another failover chain.
7. After `blocked_until`, the watcher attempts one recovery notice.
8. Recovery only clears routing after a successful recovery transition.
9. Recovery `RetryAfter` extends the block instead of falsely reporting
   success.
10. `/status` shows delivery blocked state and effective preview state.
11. `/preview on|off|status` persists and reports per-agent preference.
12. Preview disabled for `kasumi` prevents answer preview setup on future
   requests.

## Test Plan

Unit tests should cover:

1. first `RetryAfter` opens one incident and marks blocked state;
2. `send_long_message()` respects blocked state;
3. `_reply_text()` and `_send_text()` respect blocked state;
4. preview edit `RetryAfter` enters the same incident path;
5. final successful response is persisted when delivery fails;
6. default failover agent selection uses `lin_yueru` on HASHI2;
7. fallback candidate selection excludes source agent and blocked candidates;
8. dedupe window prevents failover spam;
9. failover notice cannot recurse;
10. watcher clears blocked state only after successful recovery notice;
11. recovery `RetryAfter` extends the block;
12. partial stream promotion plus continuation failure preserves undelivered
    remainder correctly;
13. `/preview on` persists true;
14. `/preview off` persists false;
15. `/preview status` reports effective source correctly;
16. `answer_stream_preview` config default is still honored when no persisted
    override exists.

Integration tests should cover:

1. Kasumi-style long Grok run with preview enabled, preview edit hits
   `RetryAfter`, undelivered final is saved, failover warning comes from
   `lin_yueru`.
2. Same scenario with `/preview off`; no preview edits are attempted and only
   final delivery can trigger the incident.
3. Block expires, recovery notice succeeds, next user message returns to normal
   delivery through the original source agent.

## Recommended Rollout Order

1. Add shared delivery state and helper module.
2. Route `_reply_text()`, `_send_text()`, and `send_long_message()` through the
   shared gate.
3. Add undelivered persistence.
4. Add watcher-based recovery.
5. Add `/preview on|off|status`.
6. Disable preview by default for `kasumi`.
7. Add status text and tests.

## Final Recommendation

The first implementation should remain intentionally simple, but it must be
simple in the right shape:

- one shared delivery gate;
- one incident state machine;
- one failover notification path;
- one persisted preview preference per agent.

That is the smallest design that actually prevents:

- silent user experience,
- repeated sends through a blocked token,
- failover notification loops,
- preview-driven Telegram edit noise on long-running Grok agents.
