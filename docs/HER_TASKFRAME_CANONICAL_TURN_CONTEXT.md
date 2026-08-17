# HER TaskFrame Canonical Turn Context

Status: implemented in HASHI1 source; automated verification passed; Python
runtime reload and recurrence live canary pending

Owner: HASHI Python Runtime + native HER Rust Runtime

Related incident: recurrence `HER-20260816-033` of `HER-20260814-030`

## 1. Decision

Keep TaskFrame as HER's planning, assurance and evidence state. Do not let it
plan from an isolated short message such as `A`, `continue` or `继续` while the
primary executor sees a different conversation.

For every direct HER request, HASHI freezes a bounded `TurnContext` at enqueue
time. The same context is then supplied to native TaskFrame planning and to the
primary execution controller.

```text
latest final turn actually delivered before enqueue
+ current user request
+ current model / effort / permission mode
+ frozen cross-session reply target, when present
                         |
                         v
              HASHI TURN CONTEXT v1
                    /            \
                   v              v
          TaskFrame planner   primary executor
```

The current user request remains the only active source of authority. Previous
dialogue resolves references; it never creates work by itself.

## 2. Why this is required

The known-bad integrated HER controller built TaskFrame requests as:

```text
messages = [current active_goal]
```

Primary execution independently used the complete persistent session. A model
switch test exposed the contradiction:

```text
TaskFrame saw:  A
Executor saw:   previous A/B choice + A
Result:         TaskFrame reported no clear task; executor completed A
```

Both calls used the newly selected model. The divergence was caused by context
selection, not by model routing.

This is a recurrence of `HER-20260814-030`. HER `.18/.19` had restored the
persistent session to planning, but a later integrated source snapshot replaced
that behavior with a one-message planner and explicitly delegated semantic
resolution to the executor. That recreated two internal interpretations of one
user turn.

## 3. HASHI-owned enqueue snapshot

`orchestrator/runtime_turn_context.py` owns the transport-visible portion of the
contract. HASHI, rather than HER, knows:

- which final Assistant message was actually delivered;
- whether a cron/isolated receipt owns a short reply;
- whether a later result arrived only after the user sent the current message;
- the selected model and effort at enqueue time;
- the current permission mode;
- whether an earlier direct turn is still pending and therefore has no visible
  final answer yet.

The latest transport-confirmed user/assistant pair is also stored per chat in a
small private workspace state file. This is not a second conversation history:
it contains one bounded exchange only, exists solely to rebuild the enqueue
snapshot after a process restart, and is cleared by explicit `/new` and `/fresh`
flows. A pre-fix workspace without this state may recover the latest complete
pair once from the existing Bridge turn store and then migrate forward.

The bounded envelope is injected as a read-only bridge section:

```json
{
  "format": "hashi-turn-context-v1",
  "captured_at_enqueue": true,
  "previous_turn_status": "captured",
  "current": {
    "request_id": "req-0002",
    "source": "telegram",
    "model": "deepseek/deepseek-v4-flash",
    "effort": "xhigh",
    "permission_mode": "workspace-write"
  },
  "reply_target": {
    "kind": "latest_delivered_final",
    "request_id": "req-0001"
  },
  "previous_turn": {
    "user_text": "choose A or B",
    "assistant_text": "A. full run\nB. dry-run"
  },
  "transition": {
    "model_changed": true,
    "effort_changed": true
  }
}
```

User and Assistant text are bounded before injection. The envelope is attached
to the queued request, so a later scheduler delivery cannot rewrite it.

### 3.1 Snapshot states

| State | Meaning | HER behavior |
| --- | --- | --- |
| `captured` | a final turn was visible when the request entered the queue, including a bounded durable recovery after restart | use that exact pair |
| `captured_no_prior_final` | an earlier direct request was still pending | do not bind to its later final |
| `unavailable` | HASHI has no process, durable, compatibility, or frozen cross-session delivery record | recover the immediate completed pair from the persistent HER session when one exists |

The durable enqueue recovery preserves Flex continuity across a full runtime
restart without requiring a persistent HER session. Native session fallback is
still available when the HASHI envelope genuinely has no previous pair. Neither
fallback is used when HASHI positively captured the absence of a visible prior
final.

## 4. HER-owned planning context

Native HER parses the envelope before appending the current turn. TaskFrame
receives only:

```text
previous user request (bounded)
previous final Assistant answer (bounded)
current authoritative user request
```

If no HASHI snapshot is available, HER derives the same immediate completed
pair from its persistent session. It does not expose the planner to an
unbounded transcript merely to resolve one short reference.

The runtime also supplies the same canonical metadata to every primary
execution iteration. Once the initial TaskFrame passes validation,
`TaskFrame.active_goal` is the canonical resolved goal for that turn. If the
TaskFrame is rejected after bounded attempts, the strict error is preserved as a
planning diagnostic and the primary Agent receives an authorization-preserving
fallback plus this same canonical context. Planning failure therefore cannot
terminate the turn or erase the primary Agent's responsibility to answer. Execution
must not reinterpret the current message into a different task.

## 5. Ambiguity and side-effect rule

For bounded short requests such as `A`, `continue`, `resume`, `ok`, `可以` and
`继续`:

- when the supplied previous dialogue determines the referent, TaskFrame must
  write the concrete resolved goal;
- an `active_goal` that merely repeats `A` or reports an obviously unclear task
  is rejected and recorded as a planning failure;
- every planning-enabled effort falls back to the primary Agent rather than
  treating that rejected TaskFrame as a terminal backend error;
- when no matching previous dialogue exists, the agent must not guess or use a
  later-delivered message as authority;
- the primary Agent may resolve the request from the same canonical previous
  dialogue and continue; if it still cannot reconcile the request with that
  context, it must avoid side-effecting tools, report the planning failure in its
  own response and ask for clarification.

This guard is deliberately narrower than a lexical intent engine. It catches
the proven control split without turning planned tool names into a brittle hard
allowlist or reintroducing the former replan loop.

## 6. Effort coverage

The native TaskFrame path is active at:

```text
medium, high, xhigh, max, max+
```

`low` does not use TaskFrame. `ultra` uses its Python orchestration plan and
disables native TaskFrame inside primary/worker calls. The HASHI enqueue
snapshot still protects ordinary direct-turn and cross-session routing outside
that native loop.

## 7. Regression coverage

Required deterministic tests cover:

1. an option turn followed by current input `A`;
2. planner messages equal `previous user + previous assistant + current user`;
3. TaskFrame and executor both receive the canonical contract;
4. HASHI enqueue snapshots preserve model/effort transitions;
5. a later cron final cannot replace a frozen reply target;
6. an earlier pending direct turn records `captured_no_prior_final`;
7. a cold HASHI runtime falls back to the persistent HER immediate pair;
8. an unresolved short-choice TaskFrame is rejected without stopping the primary
   Agent, which resolves the canonical previous dialogue or asks the user;
9. a HASHI snapshot outranks misleading newer session history;
10. the first Flex request after restart recovers the last transport-confirmed
    pair from bounded workspace state;
11. a pre-fix workspace recovers only its latest complete Bridge exchange;
12. explicit `/new` and `/fresh` clear process and durable referent state.

Source verification on 2026-08-16 completed with:

- `2297 passed, 2 skipped` in the complete HASHI Python test suite;
- all Rust workspace/all-target tests passing;
- runtime library Clippy with warnings denied passing;
- Ruff, Python compilation and `git diff --check` passing for the changed
  Python surfaces.

The workspace-wide all-target Clippy command still reaches existing lint debt
outside this change, so it is not represented as a clean release-certification
result. Offline candidate verification and the live provider canary remain part
of the adoption boundary below.

## 8. Adoption boundary

Python changes require `/reboot`. Rust changes require `/rebuild`, which must:

1. fingerprint the integrated source;
2. compile and verify an immutable development candidate;
3. leave the certified `.22` package unchanged;
4. atomically select the development candidate;
5. adopt it only into an idle authorized Agent;
6. report build, verification, adoption and health results separately.

A successful `/rebuild` is development verification, not release
certification. The native envelope consumer remains unchanged by the durable
Flex recovery; its Python runtime change requires Agent reload before the new
enqueue behavior is active. The next public package still requires the complete
clean-source, cross-platform certification and live-canary matrix.
