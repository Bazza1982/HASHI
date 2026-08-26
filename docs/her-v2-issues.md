# HER v2 Issue Register

| Field | Value |
|---|---|
| Status | Active |
| Scope | HER v2 runtime, provider adapters, tool/evidence handling, and delivery lifecycle only |
| Canonical filename | `her-v2-issues.md` |
| Last updated | 2026-08-26 |

This is the canonical register for HER v2 defects and open design gaps. General
HASHI issues belong in [KNOWN_ISSUES.md](KNOWN_ISSUES.md); implementation plans
and test plans may be linked from an entry, but they do not replace the entry's
status here.

## Register rules

- Give every issue a stable `HERV2-NNN` identifier and never reuse it.
- Keep resolved entries in the register. Record the fix reference, regression
  test, and live or integration evidence before changing an entry to
  `Resolved`.
- Treat runtime receipts and deterministic audit records as authoritative over
  model-authored summaries.
- Do not include credentials, private conversation content, or sensitive local
  paths in an entry.
- Use `Blocker`, `Major`, `Moderate`, or `Minor` for severity, and `Open`,
  `In progress`, `Resolved`, or `Accepted risk` for status. Use `Untriaged`
  while an imported finding still needs a severity decision.

## Issue index

| ID | Issue | Severity | Status | First recorded |
|---|---|---|---|---|
| [HERV2-001](#herv2-001-long-provisional-draft-is-not-replaced-after-multi-message-delivery) | Long provisional draft is not replaced after multi-message delivery | Major | Open | 2026-08-26 |
| [HERV2-002](#herv2-002-tool-activity-counts-can-diverge-from-authoritative-receipts) | Tool activity counts can diverge from authoritative receipts | Moderate | Open | 2026-08-26 |
| [HERV2-003](#herv2-003-non-zero-bash-exits-can-be-recorded-as-successful-receipts) | Non-zero Bash exits can be recorded as successful receipts | Major | Open | 2026-08-26 |
| [HERV2-004](#herv2-004-immediate-response-claims-lack-a-deterministic-performed-action-guard) | Immediate Response claims lack a deterministic performed-action guard | Untriaged | Open | 2026-08-24 |
| [HERV2-005](#herv2-005-triage-habit-selections-are-not-catalogue-bound) | Triage Habit selections are not catalogue-bound | Minor | Open | 2026-08-26 |
| [HERV2-006](#herv2-006-plan-capability-availability-is-validated-after-structured-acceptance) | Plan capability availability is validated after structured acceptance | Moderate | Open | 2026-08-26 |
| [HERV2-007](#herv2-007-sub-agent-outcome-fields-are-not-cross-checked-for-semantic-completeness) | Sub-agent outcome fields are not cross-checked for semantic completeness | Minor | Open | 2026-08-26 |
| [HERV2-008](#herv2-008-model-authored-evidence-references-are-merged-with-authoritative-receipts) | Model-authored evidence references are merged with authoritative receipts | Major | Open | 2026-08-26 |
| [HERV2-009](#herv2-009-finalisation-disclosure-is-checked-for-presence-not-canonical-state-coverage) | Finalisation disclosure is checked for presence, not canonical state coverage | Major | Open | 2026-08-26 |

## HERV2-001: Long provisional draft is not replaced after multi-message delivery

| Field | Value |
|---|---|
| Severity | Major |
| Status | Open |
| Affected flow | Max/Assured Execution to Finalisation delivery |
| Evidence | Hashi2 `arale`, turn `req-0001-019e2f651d05`, observed 2026-08-26 |

### Expected behaviour

At Max/Assured effort, a provisional Execution draft may be shown only when the
transport proves that Runtime can later resolve it in place. Finalisation must
replace or otherwise fully remove the provisional material. If that guarantee
is unavailable, the user should receive one ordinary final response.

### Observed behaviour

The provisional Execution draft was about 4,157 characters and was delivered
as two Telegram messages. The send was accepted, but the per-message transport
identifiers were not retained as one resolvable provisional delivery. Runtime
therefore rejected the in-place resolution with
`provisional_resolution_rejected` and sent the Finalisation response as a new
multi-message delivery. The long draft remained visible beside the formal
result.

### Impact

The user receives duplicate and potentially conflicting answers, and an
unreviewed Execution draft remains visible after Review and Finalisation. This
breaks the assured-delivery contract even though the later stages themselves
complete.

### Likely mechanism

`RuntimePipeline._send_event()` uses the long-message path for rendered text
over the transport threshold. That path can return multiple results.
`_transport_accepted()` recognises a list or tuple as accepted, while
`_transport_message_id()` does not extract and retain the corresponding set of
message identifiers. `_initial_resolution_presenter()` then has no complete
provisional record to resolve.

### Required resolution

Choose and enforce one deterministic policy for multi-message provisional
content:

1. retain every provisional message identifier and replace or delete the whole
   set before publishing the formal result; or
2. suppress provisional delivery whenever the complete multi-message result
   cannot be resolved safely, then publish only the formal result.

### Acceptance evidence

- A regression test covers a provisional response above the single-message
  threshold.
- After Finalisation, no stale draft chunk remains visible.
- Transport edit/delete failure follows a deterministic fallback without
  duplicating the full response.
- The audit record identifies every provisional message and its terminal
  resolution.

### Code and contract pointers

- `orchestrator/runtime_pipeline.py`: `_send_event()`,
  `_transport_message_id()`, and `_initial_resolution_presenter()`
- `HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md`: provisional
  Execution-draft and Finalisation replacement contract

## HERV2-002: Tool activity counts can diverge from authoritative receipts

| Field | Value |
|---|---|
| Severity | Moderate |
| Status | Open |
| Affected flow | Provider activity summaries, Review input, and audit presentation |
| Evidence | Hashi2 `arale`, turn `req-0001-019e2f651d05`, observed 2026-08-26 |

### Expected behaviour

Tool start and completion totals must reconcile with unique authoritative tool
receipts. Retransmitted, specialised, or interleaved provider events must not
create another logical tool call.

### Observed behaviour

The Execution activity summary reported three `verification_run` completions,
while the authoritative receipt set contained two. Its completion counts
totalled 21 against 20 receipts. Review also showed inflated start counts for
some tool families. The compulsory Replan used the exact receipt set in this
turn, so the discrepancy did not alter its control-flow decision.

### Impact

Progress displays and model-facing summaries can overstate performed work and
make audit reconciliation unreliable. The authoritative receipts remained
intact in the observed turn, limiting this instance to reporting and review
context rather than execution control.

### Likely mechanism

`ProviderActivityTracker` aggregates provider events by effective tool name and
uses event ordering heuristics to suppress adjacent duplicate start forms. It
does not use a stable tool-call identifier as the primary deduplication key, so
duplicate or interleaved stream events can be counted more than once. This
mechanism must be confirmed by a focused replay test before it is treated as
the final root cause.

### Required resolution

- Deduplicate tool starts and completions by stable tool-call identity.
- Derive final completion totals from unique authoritative receipts, or
  deterministically reconcile the activity stream against them.
- Surface a reconciliation error instead of silently publishing conflicting
  totals.

### Acceptance evidence

- Tests cover duplicate, adjacent specialised, and interleaved provider
  events.
- Each logical call contributes at most one start and one terminal completion.
- Per-tool and aggregate counts equal the unique receipt ledger at stage end.
- Replan, Review, and user-visible progress consume explicitly labelled
  authoritative or non-authoritative counts.

### Code pointers

- `orchestrator/her_v2/progress.py`: `ProviderActivityTracker`
- HER v2 provider activity and evidence-receipt integration tests

## HERV2-003: Non-zero Bash exits can be recorded as successful receipts

| Field | Value |
|---|---|
| Severity | Major |
| Status | Open |
| Affected flow | Bash tool result semantics and HER v2 evidence receipts |
| Evidence | Hashi2 `arale`, turn `req-0001-019e2f651d05`, observed 2026-08-26 |

### Expected behaviour

A completed Bash process with exit code zero is a successful tool receipt. A
non-zero exit, timeout, policy denial, cancellation, or launch failure must be
recorded with the corresponding non-success semantics while preserving the
command output as evidence.

### Observed behaviour

At least two Bash results began with `[exit code 1]`, but their HER v2 evidence
receipts were recorded as `SUCCESS`. A timed-out Bash call in the same flow was
correctly recorded as `FAILED`.

### Impact

The authoritative receipt ledger can assert success for a failed command.
Replan summaries, Review, Verification, and later audit consumers may therefore
treat negative evidence as completed successful work. The model recovered from
the failed commands in the observed turn, but that does not repair the receipt
semantics.

### Root cause

`execute_bash()` renders a non-zero process result as ordinary output prefixed
with `[exit code N]`. `ToolRegistry` currently treats output as an error only
when its text starts with `Error:`. The HER v2 evidence wrapper maps
`is_error=false` to a `SUCCESS` receipt, so a non-zero shell exit crosses the
tool boundary without structured failure state.

### Required resolution

- Carry structured `exit_code` and command-success state in the built-in tool
  result.
- Set non-success receipt semantics for every non-zero exit without discarding
  stdout or stderr.
- Keep process exit, timeout, policy denial, cancellation, and adapter failure
  distinguishable in receipt details.

### Acceptance evidence

- Exit code `0` produces a `SUCCESS` receipt.
- A representative non-zero exit produces a `FAILED` receipt with the exact
  exit code and retained output.
- Timeout, policy denial, cancellation, and launch failure retain their own
  deterministic classifications.
- Replan, Review, and Verification receive the corrected structured status.

### Code pointers

- `tools/builtins.py`: `execute_bash()` and `BuiltinExecutionResult`
- `tools/registry.py`: tool-result error classification
- `adapters/her_v2_provider.py`: `_EvidenceRecordingToolRegistry.execute()`

## HERV2-004: Immediate Response claims lack a deterministic performed-action guard

| Field | Value |
|---|---|
| Severity | Untriaged |
| Status | Open |
| Affected flow | Immediate Response |
| Source | Migrated from `HASHI_UNRELEASED_CHECKPOINT_2026-08-24.md` |

### Open design gap

Prompt instruction exists, but Runtime does not yet have a deterministic guard
that prevents an Immediate Response from claiming an action that was not
performed. The checkpoint explicitly left this as a separate open design item
and did not claim that prompt wording made the case impossible.

### Required resolution and acceptance evidence

- Define the deterministic claim/receipt invariant for Immediate Response.
- Reject or correct action claims that have no matching authoritative evidence.
- Add focused regression coverage for a claimed but unperformed action.
- Record the implementation, test, and live or integration evidence here before
  resolving the entry.

### Contract pointer

- `HASHI_UNRELEASED_CHECKPOINT_2026-08-24.md`: "Known limits and unclaimed
  evidence"

## HERV2-005: Triage Habit selections are not catalogue-bound

| Field | Value |
|---|---|
| Severity | Minor |
| Status | Open |
| Affected flow | Triage output propagated into Planning and Execution prompts |
| Disposition | Future consideration; no new validation gate is currently authorised |

### Open design gap

`relevant_habits` is validated as a duplicate-free list of non-empty strings,
but each value is not checked against the retrieved Habit catalogue. These are
complete Habit strings rather than IDs, and accepted values are passed onward
as advisory prompt content. No tool permission or direct execution authority is
derived from the field.

### Future consideration

If observed model behaviour makes this material, compare selected values with
the exact retrieved catalogue at the existing Triage boundary. Do not add a
separate stage or recovery path solely for this advisory field.

### Code pointers

- `orchestrator/her_v2/structured.py`: `parse_triage()`
- `orchestrator/her_v2/runtime_support.py`: `_record_triage()`
- `orchestrator/her_v2/prompts.py`: Planning and Execution prompt rendering

## HERV2-006: Plan capability availability is validated after structured acceptance

| Field | Value |
|---|---|
| Severity | Moderate |
| Status | Open |
| Affected flow | High-volume Planning and Replanning delegation |
| Disposition | Future consideration; runtime authority enforcement remains active |

### Open design gap

Planning and Replanning validate the shape of sub-agent profiles, tools, and
attachment IDs before accepting the structured plan. Availability is checked
later while Runtime builds or invokes the sub-agent batch. An unavailable
profile or attachment can therefore fail after JSON Repair is no longer
available, while unknown tool names are narrowed out by the delegated registry.

The observed mechanism does not grant unavailable tools or attachments. Its
impact is late failure or silent capability reduction rather than authority
expansion.

### Future consideration

If this becomes operationally frequent, move the existing availability checks
into the current context-aware Planning/Replanning validator. Do not create a
new lifecycle stage or a second planning gate.

### Code pointers

- `orchestrator/her_v2/structured.py`: `_validated_delegation_plan_fields()`
- `orchestrator/her_v2/runtime.py`: `_subagent_batch()`
- `adapters/her_v2_provider.py`: `_DelegatedToolRegistry`

## HERV2-007: Sub-agent outcome fields are not cross-checked for semantic completeness

| Field | Value |
|---|---|
| Severity | Minor |
| Status | Open |
| Affected flow | Structured sub-agent Execution results |
| Disposition | Future consideration; avoid adding semantic gates without observed harm |

### Open design gap

The sub-agent Execution parser requires a valid disposition and summary, and a
clarification for `USER_INPUT_REQUIRED`. It does not require a non-empty
`limitations` field for `COMPLETED_WITH_LIMITATIONS`, require `remaining_work`
for `FAILED`, or reject a clarification attached to a completed result. The
summary may already carry that meaning, so enforcing field combinations would
be a new semantic gate rather than a straightforward parser correction.

### Future consideration

Keep this as an evidence-driven design item. Add deterministic cross-field
requirements only if real executions show that Review and aggregation cannot
reliably interpret the accepted outcomes.

### Code pointer

- `orchestrator/her_v2/structured.py`: `_parse_execution_data()`

## HERV2-008: Model-authored evidence references are merged with authoritative receipts

| Field | Value |
|---|---|
| Severity | Major |
| Status | Open |
| Affected flow | Sub-agent result evidence aggregation |

### Expected behaviour

Only references already known to the request ledger, Tool Gateway, or another
explicitly authoritative evidence source should be represented as established
evidence. Model-authored references may be retained as claims but must not gain
authority merely by appearing in JSON.

### Current behaviour

The structured sub-agent outcome accepts arbitrary string `evidence_refs`.
Runtime merges those strings with the provider response's authoritative receipt
references without reconciling their provenance.

### Future consideration

Filter model-authored references against the known evidence ledger, or keep
them in a separately labelled claim field. Unknown references should be
ignored or audited rather than merged as authoritative evidence. This can be a
provenance normalisation and does not require another model gate.

### Code pointers

- `orchestrator/her_v2/structured.py`: `_parse_execution_data()`
- `orchestrator/her_v2/runtime.py`: `_invoke_subagent()`

## HERV2-009: Finalisation disclosure is checked for presence, not canonical state coverage

| Field | Value |
|---|---|
| Severity | Major |
| Status | Open |
| Affected flow | Reviewed/Assured Finalisation and user-visible closure |

### Expected behaviour

Finalisation should not contradict or omit material canonical Execution,
Review, assurance, limitation, or technical-unavailability state supplied by
Runtime.

### Current behaviour

The parser requires a non-empty final message and validates an optional legacy
execution envelope. It does not deterministically establish that the visible
message covers every material canonical state. A fluent but incomplete message
can therefore pass.

### Future consideration

Prefer deterministic runtime composition of a concise canonical status footer
over rejecting and replaying a completed Finalisation model call. Any change
should avoid another semantic wording gate or another model stage.

### Code pointers

- `orchestrator/her_v2/structured.py`: `parse_finalisation()`
- `orchestrator/her_v2/runtime.py`: Finalisation terminal-state resolution

## Migration note

The active HER v2 design, testing, cleanup, and checkpoint documents were
checked when this register was created. They are contracts, implementation
plans, or point-in-time evidence rather than an issue register. HERV2-004 is the
one explicit unresolved HER v2 design item migrated from the current integration
checkpoint. Pending rollout canaries are evidence gaps, not defects, and were
not converted into issues without a failing observation.
