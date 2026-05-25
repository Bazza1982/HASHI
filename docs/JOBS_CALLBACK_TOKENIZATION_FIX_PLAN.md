# `/jobs` Callback Tokenization Fix Plan

**Status:** Implemented on `hashi_remote_fix` on 2026-05-26  
**Implementation reference:** `docs/HASHI_REMOTE_FIX_BUNDLE_2026-05-26.md`

This document started as a fix plan. The design described here has now been
implemented for both `/jobs` and `/nudge`.

## 1. Summary

This document explains the current `/jobs` failure in HASHI2, why it is not
safe to fix it by merely shortening one job id, and what permanent
function-layer change should be implemented so the fix is stable, mergeable,
and safe to bring back to `main`.

The short version:

- `/jobs` itself is still registered and callable.
- The failure happens when `/jobs` tries to send inline buttons to Telegram.
- The current implementation embeds the full `job_id` directly into
  `callback_data`.
- Telegram limits `callback_data` to 64 bytes.
- The new watchdog heartbeat id
  `lin_yueru-loop-hashi-remote-watchdog-7d` pushes several `/jobs` buttons over
  that limit.
- As soon as one button exceeds the limit, Telegram rejects the whole message.

This is not a one-off watchdog naming problem. It is a structural flaw in the
operator callback design.

## 2. Nature Of The Issue

### 2.1 Where the failure lives

The `/jobs` command entrypoint still exists:

- `orchestrator/flexible_agent_runtime.py`
  - `cmd_jobs()`

The command renders its UI through:

- `orchestrator/runtime_jobs.py`
  - `_build_jobs_with_buttons()`

That helper currently constructs inline button callbacks like:

- `skilljob:heartbeat:run:<job_id>:now`
- `skilljob:heartbeat:toggle:<job_id>:off`
- `skilljob:heartbeat:transfer:<job_id>:select`
- `skilljob:heartbeat:delete:<job_id>:confirm`

This assumes `job_id` is always short enough to fit into Telegram's 64-byte
limit for `callback_data`.

That assumption is false.

### 2.2 Why it failed now

Older heartbeat ids were short:

- `lin_yueru-loop-29ba4d`
- `lin_yueru-loop-70caa5`

Those callbacks stayed under the limit.

The new watchdog heartbeat id is longer:

- `lin_yueru-loop-hashi-remote-watchdog-7d`

Actual callback lengths now become:

- `run` = 66
- `toggle` = 69
- `transfer` = 74
- `delete` = 73

Once these exceed Telegram's limit, the outgoing `/jobs` message becomes
invalid and Telegram rejects it.

### 2.3 Why this is a design bug, not an operator error

The operator request that created the watchdog loop was valid.

The system accepted the heartbeat job, stored it in `tasks.json`, and the
scheduler successfully ran it. That means:

- the job model accepts long ids,
- the scheduler accepts long ids,
- only the `/jobs` UI path breaks.

So the mismatch is inside the UI callback protocol, not in the loop request.

### 2.4 Why shortening the watchdog id is not the real fix

Renaming this one job would only hide the current symptom.

It would not address the underlying flaw:

> `/jobs` assumes every future job id can safely fit inside Telegram
> `callback_data`.

That is not a safe invariant for:

- user-generated `/loop` ids,
- long transferred job ids,
- imported jobs,
- future managed jobs,
- jobs with instance-aware naming,
- jobs carrying workflow or watchdog semantics.

So shortening one id is a tactical workaround, not a permanent fix.

### 2.5 Why this is broader than `/jobs`

`/nudge` currently uses the same raw-id callback pattern:

- `nudgejob:trigger:<job_id>:now`
- `nudgejob:toggle:<job_id>:off`
- `nudgejob:delete:<job_id>:confirm`

So `/jobs` is the currently visible failure, but the underlying protocol flaw
affects another operator panel too. Fixing only `/jobs` would knowingly leave a
second Telegram 64-byte failure path in place.

## 3. Current Failure Mode

### 3.1 User-visible behavior

From the user's perspective:

- `/jobs` appears to "stop working"
- no usable jobs panel appears
- it can feel like the command itself is broken

### 3.2 Internal behavior

Internally, the likely flow is:

1. `/jobs` runs normally.
2. `_build_jobs_with_buttons()` builds valid text.
3. One or more generated button callbacks exceed Telegram's 64-byte limit.
4. Telegram rejects the message payload containing the inline keyboard.
5. The user never receives the jobs panel.

The same structural failure would happen for `/nudge` once a nudge job id grows
past the same threshold.

### 3.3 Why this matters operationally

This is not just cosmetic. `/jobs` is one of the main operator control panels
for:

- viewing heartbeats
- viewing crons
- toggling jobs on/off
- running jobs manually
- transferring jobs
- deleting jobs

If `/jobs` fails, the control plane for scheduled work becomes unreliable.

## 4. Design Goal For The Permanent Fix

The permanent fix must satisfy all of the following:

1. Preserve existing job ids unchanged.
2. Keep `tasks.json` semantics unchanged.
3. Avoid touching scheduler core behavior.
4. Keep the change in the function/UI layer where the bug actually lives.
5. Guarantee `callback_data <= 64` for all `/jobs` and `/nudge` buttons.
6. Be safe for future long job ids.
7. Be stable enough to merge back into `main`.

## 5. Proposed Permanent Fix

### 5.1 Core idea

Stop embedding full `job_id` directly in Telegram `callback_data`.

Instead:

- generate a short-lived local token
- store a mapping from token -> callback operation context
- send only the short token in `callback_data`
- resolve the token server-side when the callback is received

This is the same general principle already used elsewhere in HASHI for transfer
flows, where compact callback keys are used instead of dumping all semantics
into the raw callback string.

### 5.2 New callback shape

Instead of:

- `skilljob:heartbeat:run:lin_yueru-loop-hashi-remote-watchdog-7d:now`

Use:

- `skilljob:heartbeat:key:j1a3f2:run`

The callback family should be:

- `skilljob:<kind>:key:<token>:run`
- `skilljob:<kind>:key:<token>:toggle`
- `skilljob:<kind>:key:<token>:delete`
- `skilljob:<kind>:key:<token>:transfer`

Transfer step 2 should remain unchanged:

- `skilljob:<kind>:xferkey:<token>:go`

That keeps the new design aligned with an already-proven callback token pattern
in this codebase rather than inventing a second transport model.

The backing token store would contain something like:

```json
{
  "j1a3f2": {
    "kind": "heartbeat",
    "task_id": "lin_yueru-loop-hashi-remote-watchdog-7d",
    "action": "run",
    "created_at": "2026-05-25T14:00:00+10:00",
    "agent": "lin_yueru"
  }
}
```

The design principle is:

- short callback
- local mapping
- explicit expiry

### 5.3 Where the mapping should live

The cleanest function-layer approach is:

- keep token generation and token resolution inside:
  - `orchestrator/runtime_jobs.py`
  - `orchestrator/runtime_nudge.py`
- keep the runtime instance responsible for storing the active callback map

Possible storage options:

1. in-memory runtime cache
2. small transient JSON file under workspace/runtime state

Recommended first step:

- **in-memory cache with timestamp-based expiry**

Why:

- this bug only affects live interactive Telegram callbacks
- callbacks are short-lived by nature
- they do not need durable cross-restart storage
- this keeps the change small and local

### 5.4 Required behavior for the token store

The token store should:

- generate compact collision-resistant keys
- resolve keys to `{kind, task_id, action, extra}`
- expire old keys automatically
- be safe if the user presses an old button after a restart

The token generator should **not** reuse a simple `len(store)+1` counter.

Reason:

- a cleared store can recreate old ids,
- stale inline buttons may still exist in Telegram,
- a reused token could point at the wrong job action.

Recommended token shape:

- short random hex, e.g. `j` + `uuid.uuid4().hex[:6]`

Expired or unknown token behavior should be:

- respond with a clear alert such as:
  - `"This jobs action expired. Open /jobs again."`

This is better than letting old inline buttons fail silently.

### 5.5 Backward compatibility strategy

Do **not** remove the existing raw callback branches immediately.

Instead:

- add a new `key` branch for tokenized callbacks,
- keep the legacy raw branches in place for rollout safety,
- stop emitting legacy raw callbacks from the UI after tokenization lands.

Why:

- previously-sent short callbacks may still be in flight during deployment,
- those old callbacks were accepted by Telegram, so they were already short
  enough to be valid,
- keeping the old branches reduces upgrade risk without widening scheduler
  scope.

This means the migration model is:

1. add `key` support,
2. keep old raw callback handling,
3. emit only tokenized callbacks from the panel.

## 6. Proposed Code-Level Change Scope

This should stay limited to the function/UI layer and avoid scheduler core.

### 6.1 Files expected to change

Primary:

- `orchestrator/runtime_jobs.py`
- `orchestrator/runtime_nudge.py`

Likely touchpoints:

- `orchestrator/flexible_agent_runtime.py`
  - only if a small runtime-owned callback map helper is needed
- `tests/test_agent_runtime_job_transfer.py`
  - pattern already relevant because it validates short callback payloads
- a new or updated jobs-specific test file for `/jobs` buttons
- a new or updated nudge-specific test file for long callback payloads

### 6.2 Files that should *not* need behavioral changes

- `orchestrator/scheduler.py`
- `tasks.json` schema
- `orchestrator/skill_manager.py`

Those are not the source of the bug.

## 7. Detailed Plan

### Phase 1. Reproduce and lock the failing case in tests

Add regression tests that build operator panels with long ids and assert:

- every generated `callback_data` is `<= 64`
- the long id still appears in visible text
- the command can still render all buttons

This test set must fail under the current implementation and pass after the
fix.

Include both:

- `/jobs`
- `/nudge`

in the same change set, not as separate later cleanups.

### Phase 2. Introduce compact callback tokens for `/jobs`

Implement a token registry for `/jobs` actions:

- create token
- bind token to `{kind, task_id, action, metadata}`
- generate short callback strings
- update callback parser to resolve token before dispatching
- add a `key` callback branch instead of replacing the parser shape entirely

After token resolution, dispatch into the existing run/toggle/delete/transfer
logic rather than duplicating business behavior.

### Phase 3. Preserve existing `/jobs` UX

Do not change the visible UI semantics.

Buttons should still be:

- Run
- ON/OFF
- Transfer
- Delete

Only the callback transport encoding should change.

The existing transfer second-step token flow should remain unchanged:

- `skilljob:<kind>:xferkey:<token>:go`

### Phase 4. Expiry, cleanup, and stale-button handling

Add explicit stale-button behavior:

- if token missing or expired, answer callback with a short alert
- instruct user to reopen `/jobs`

Recommended first token policy:

- TTL around 30 minutes
- cap around 256 live entries per runtime
- prune on create and on lookup

If the store must be pruned heavily or reset due to cap pressure, log that at
`INFO`.

### Phase 5. Apply the same fix to `/nudge`

Replace raw nudge callback strings such as:

- `nudgejob:trigger:<job_id>:now`
- `nudgejob:toggle:<job_id>:off`
- `nudgejob:delete:<job_id>:confirm`

with the same short-token model, and add the corresponding `key` branch for
callback resolution.

Keep legacy raw nudge callback handling for rollout safety, just as with
`/jobs`.

### Phase 6. Regression testing

Test matrix should include:

1. short heartbeat id
2. long heartbeat id
3. long cron id
4. transfer button
5. delete button
6. toggle button
7. expired token behavior
8. short nudge id
9. long nudge id
10. legacy raw callback branch still works for already-short ids

Every emitted `callback_data` in both `/jobs` and `/nudge` should be asserted
to stay within Telegram's 64-byte limit.

### Phase 7. Logging and observability

Add focused logs only where they improve diagnosis:

- `DEBUG`: token created with kind/action/task id
- `DEBUG`: legacy raw callback branch used
- `INFO`: token store pruned or reset
- `WARNING`: token missing, expired, or malformed

There is no need to log every successful token resolution.

Code comments should also make the Telegram constraint explicit, especially
where callback strings are assembled.

### Phase 8. Manual verification

After code change:

1. open `/jobs`
2. verify the panel renders
3. press:
   - Run
   - Toggle
   - Delete confirm
   - Transfer
4. confirm all still work with the long watchdog job present
5. open `/nudge`
6. verify the panel also renders and its buttons still work with long ids

## 8. Why This Fix Is Safe To Take Back To `main`

This proposal is a good candidate for `main` because:

- it addresses a general protocol flaw, not a local one-off
- it reduces coupling between UI callback size and job id length
- it does not change task semantics
- it does not alter scheduler execution behavior
- it strengthens operator control-plane reliability
- it matches an existing HASHI design pattern: compact callback keys with local
  resolution

In short:

- **small blast radius**
- **clear regression test**
- **high confidence**
- **future-proof**

## 9. Risks And Mitigations

### Risk 1. Token expiry makes old buttons stop working

This is acceptable and preferable to silently broken callbacks.

Mitigation:

- clear alert:
  - `"This jobs action expired. Open /jobs again."`
- bounded TTL so stale buttons lose validity quickly

### Risk 2. In-memory token store is lost on restart

Also acceptable for interactive Telegram callbacks.

Mitigation:

- stale-button fallback as above
- do not widen scope to persistence unless a real operational need appears

### Risk 3. Partial migration leaves some buttons using raw job ids

Mitigation:

- centralize callback construction through one helper per panel
- test every button type
- include `/nudge` in the same PR

### Risk 4. Token collision maps an old button to the wrong action

Mitigation:

- use short random tokens instead of simple counters
- keep TTL bounded
- store `kind`, `task_id`, and intended action together
- validate resolved payload before dispatch

## 10. Alternatives Considered

### Alternative A. Rename the watchdog job to a shorter id

Rejected as permanent fix.

Reason:

- solves only today's failure
- future long ids can break `/jobs` again
- not suitable for `main`

### Alternative B. Truncate `job_id` inside `callback_data`

Rejected.

Reason:

- ambiguous mapping
- risk of collisions
- weak operator safety

### Alternative C. Encode job ids with hashing only

Possible, but token mapping is still needed for collision-safe action routing.

So plain hashing alone is not enough unless it effectively becomes a token
system anyway.

### Alternative D. Fix `/jobs` now and `/nudge` later

Rejected.

Reason:

- it leaves another already-known Telegram callback overflow bug in place
- the callback pattern is almost identical
- the same helper and tests can cover both surfaces in one safe PR

## 11. Recommendation

Recommended path:

1. Keep the watchdog job id unchanged.
2. Fix `/jobs` and `/nudge` at the function layer by tokenizing callbacks.
3. Add a new `key` callback branch while keeping legacy raw branches for rollout
   safety.
4. Use short random tokens with TTL and bounded store cleanup.
5. Add regression tests for long ids and callback length limits.
6. Manually verify `/jobs` and `/nudge` rendering and button actions.
7. Only after that, consider this safe to merge back to `main`.

## 12. Review Questions

1. Is in-memory token storage sufficient for the first permanent fix, or is
   there a strong operational reason to survive restart?
2. Confirm that `/nudge` should ship in the same PR rather than as a follow-up.
3. Is a 30-minute TTL and ~256-entry cap a reasonable first policy for button
   token storage?
4. Is there any preferred runtime-owned place for the transient callback map if
   we want to keep the helper logic out of UI rendering functions?
