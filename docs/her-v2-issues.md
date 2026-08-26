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

## Migration note

The active HER v2 design, testing, cleanup, and checkpoint documents were
checked when this register was created. They are contracts, implementation
plans, or point-in-time evidence rather than an issue register. HERV2-004 is the
one explicit unresolved HER v2 design item migrated from the current integration
checkpoint. Pending rollout canaries are evidence gaps, not defects, and were
not converted into issues without a failing observation.
