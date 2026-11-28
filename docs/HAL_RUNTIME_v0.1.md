# HAL v0.1 Runtime — HASHI Implementation Notes

This guide describes HAL on HASHI main. Workbench Canvas is the editor and
observer. The HASHI instance that owns a saved, enabled rule runs it and
stores its receipts. Source availability does not establish adoption on an
installed instance. For the original language design see [HAL v0.1](HAL_SPEC_v0.1.md).

## Start with a Canvas rule

1. In Workbench Canvas add a **HAL** workflow block and open its Inspector.
2. Choose the verified HASHI connection that will own the rule. Write a rule,
   then **Validate** it and review the available events.
3. **Save** the rule to that instance and **Enable** it explicitly. Review
   recent executions and per-step receipts in the same Inspector. Canvas edits
   can be saved locally before the HASHI runtime is reachable.
4. Use **Disable** to stop new triggers for that rule. A peer remains a
   destination for individual prompts. It does not become the rule owner.

This example uses local Agent names; replace both with Agents in the owning
instance. Use a peer-qualified destination only when its Remote route and
credentials have been verified.

```text
if @builder #turn_finish then @reviewer $prompt "Review the completed change.".
```

## Architecture

HAL is an optional Layer-2 Function with zero effect when unused:

- **`hal/language.py`** — author-owned deterministic compiler (unchanged by the
  runtime work; already shipped on this branch).
- **`hal/event_source.py`** maps Run events from the existing durable event
  feed and Whiteboard transitions from the configured board feed. Each source
  keeps its own cursor.
- **`hal/runtime.py`** — pure planning engine: entry matching, per-execution
  `@agent_trigger` binding, `.`/`;` semantics, correlation, honest failure
  planning. No I/O.
- **`hal/store.py`** stores rules, receipts and consumed-event cursors under
  `<bridge_home>/state/hal/hal_v1.sqlite`. On upgrade, a legacy store at
  `<bridge_home>/hal/hal_v1.sqlite` is copied to the state directory when the
  new store is absent. A failed migration keeps the legacy store in use.
- **`hal/dispatch.py`** — execution adapters. Prompts ride the existing
  `runtime.enqueue_api_text` admission with a `hal:<exec>:<stage>` idempotency
  key; native commands ride `try_execute_slash_command_text`. HAL never
  implements a second executor.
- **`hal/service.py`** — runtime loop + Workbench API handlers, hosted by the
  existing `WorkbenchApiServer` process.

## Cross-instance prompt chains

The instance hosting an enabled rule owns the workflow and its durable
receipts. Rule entry still observes that instance's event feed. A qualified
peer target such as `@temp@HASHI7` uses the configured HASHI Remote peer
directory and shared token. The sender verifies the destination before
sending the prompt. Remote checks the signed source and expected destination
before forwarding, and signs the returned receipt.

The receiving PAO admits the prompt in the target Agent's stable activity
Session using the original HAL idempotency key. The rule owner reads only
that Agent and Run's terminal evidence over the same authenticated route.
Dependent stages check the recorded target and correlation key before
advancing. An unavailable peer leaves a waiting diagnostic or a bounded
retry; reconnect reconciles against persisted evidence without redispatching
a completed operation. Unreadable or invalid peer credentials follow the
same per-stage unavailable path, so local rules keep running. Run detail
identifies the instance of every prompt receipt. Cross-instance native
commands and remote entry subscriptions remain outside this capability.

## Whiteboard stage transitions

Set `HASHI_HAL_WHITEBOARD_URL` on the rule-owning instance. The default is
`http://127.0.0.1:4417`. An empty value disables the source. For a board in
key mode, set `HASHI_HAL_WHITEBOARD_TOKEN` privately. The board must expose the
stage-transition feed and its stage table.

```text
if #whiteboard_stage where [project="Hashi", to_stage="in_review"] then @{reviewer} $prompt "Review {pr_url} at {head_sha} for {work_key}".
```

- Use the exact project and stage keys configured on your board. The optional
  `where` clause accepts `project`, `from_stage`, `to_stage` and `category`.
  Values are exact matches. A missing field fails the match.
- `@{implementer}`, `@{reviewer}` and `@{tester}` use the role assigned to that
  card. A role can name a local Agent or a trusted peer. Missing assignments
  are reported on the affected step.
- Prompt variables include `{card_id}`, `{work_key}`, `{revision}`, `{round}`,
  `{max_rounds}`, `{pr_url}`, `{head_sha}`, `{last_verdict}`, `{test_env}` and
  `{next_step}`. The card context is saved with the execution.
- Enabling reads the board's current position before accepting the rule. It
  does not replay existing card history. An unavailable board blocks a new
  Whiteboard rule from being enabled, and an existing rule can still be disabled.
- Fused or halted cards and over-budget rework settle affected executions.
  The operator can inspect the run or notice and the corresponding board
  comment when comment delivery succeeds.

## Event availability

| HAL event | Source | Status |
|---|---|---|
| `#turn_finish` | real feed `run.completed` | available |
| `#turn_error` | real feed `run.failed` | available |
| `#whiteboard_stage` | configured Whiteboard transition feed | available when the board supports the source |
| `#system_idle` | no reliable active-to-idle event source | **unavailable** |
| `#turn_commentary` | no commentary events in the durable feed | **unavailable** |
| `#hashideploy_finish` | no deployment-completion event source | **unavailable** |
| `#reboot_ok` | no reboot-completion event source | **unavailable** |

Validation reports per-stage availability and never pretends an unavailable
event can trigger. A workflow may be enabled when its **entry** event is
available; stages waiting on unavailable events stay `active` with an honest
detail (`waiting for #reboot_ok (no reboot-completion event source exists)`).

## Semantics decisions (all evidence-based, no new vocabulary)

1. **No history replay.** Enabling a rule anchors the per-owner feed cursor at
   enable time; only events committed afterwards can trigger.
2. **Recursion guard.** Runs dispatched by automation carry `hal:` or `hea:`
   idempotency prefixes; the feed marks them `automation_origin` and entry
   matching skips them. Dependent stages may still correlate their own
   dispatched run.
3. **`;` correlation.** A dependent stage advances only when the triggering run's
   idempotency key equals the previous stage's dispatch key. Unrelated runs —
   including other deployments — never advance the chain. System-event stages
   (`#system_idle`, `#reboot_ok`, ...) carry no per-workflow correlation
   evidence and therefore never auto-advance.
4. **`@agent_trigger` binding.** Bound per execution from the triggering event's
   canonical identity (`@name@instance`), never through a shared global. On
   system events there is no trigger Agent; a prompt referencing
   `@agent_trigger` fails that stage honestly.
5. **Committed routes and source matching.** At startup or shared handoff, the HAL loop leaves feed cursors unchanged until
   the shared generation commits its Worker routes. A known Agent starting
   individually may use the existing bounded, idempotent prompt retry.
   Source names match case-insensitively. `@name` selects a local source and
   `@name@instance` also checks the instance qualifier.
6. **Native commands.** Executed through the existing slash-command path with
   the trigger Agent's runtime (or, for system events, the deterministic first
   runtime by sorted name). Native commands have no idempotency key: a failed
   or ambiguous outcome is **never auto-retried** and is recorded honestly for
   manual verification.
7. **Prompt retries.** Prompt dispatch is idempotent via the run idempotency
   key; transient failures retry with the same key until accepted.
8. **Crash recovery.** Receipts are written before dispatch; replays reconcile
   stage/state divergence without double execution. Command receipts left
   `unknown` by an interruption are resolved as failed-with-note, never re-run.
9. **Zero cost when disabled.** With no enabled rules there is no loop task and
   no polling. The loop exits when the last rule is disabled. Enabled rules
   cost one bounded feed check per ≤5s (the existing HEA long-poll precedent),
   plus event-driven wake.

## API (authenticated Workbench API, owner-scoped, admin-only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/hal/status` | availability + enabled-rule count + per-event availability |
| POST | `/api/v1/hal/validate` | compile + availability diagnostics (never persists) |
| GET | `/api/v1/hal/rules` | rule list with validation summaries |
| POST | `/api/v1/hal/rules` | create/update (revision-aware, 409 on conflict) |
| POST | `/api/v1/hal/rules/{id}/enable` | enable (entry event must be available) |
| POST | `/api/v1/hal/rules/{id}/disable` | disable |
| GET | `/api/v1/hal/runs` | workflow executions |
| GET | `/api/v1/hal/runs/{exec_id}` | execution detail with receipts |
| GET | `/api/v1/hal/notices` | owner-scoped Whiteboard notices |
| POST | `/api/v1/hal/peer` | authenticated peer prompt admission or keyed terminal Run evidence |

All endpoints reuse the existing admin authentication (`_v1_owner_id` +
`_check_admin_auth`), the same trust boundary as the HEA event feed. Request
bodies are bounded (64 KiB scripts, 413 before unbounded reads).

## Runtime boundaries

HAL runs in the existing HASHI Functions service. With no enabled rules it
does not poll. It uses the configured routes and existing authenticated
admission paths. Workbench authors and observes rules; the runtime owns
event consumption, dispatch, and durable outcomes.
