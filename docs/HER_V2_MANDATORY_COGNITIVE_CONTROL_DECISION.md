# HER v2 Mandatory Cognitive Control Decision

| Field | Accepted value |
|---|---|
| Status | Accepted; permanent safety invariant |
| Date | 2026-09-06 |
| Scope | Every HER v2 Agent, provider, execution mode, and tool-enabled lifecycle stage |
| Decision owner | HER v2 Engine runtime |
| Supersedes | Optional Agent rollout gate `cognitive_control_enabled` |
| Parent design | [HER v2 Product Requirements and Technical Design](HER_V2_PRODUCT_REQUIREMENTS_AND_TECHNICAL_DESIGN.md) |

## Decision

Provider-neutral cognitive control is permanently active in HER v2. It is not
an optional capability and cannot be disabled by configuration, constructor
argument, Agent preference, execution mode, provider profile, or task route.

Every HER v2 Turn creates one lifecycle-wide evidence-aware `TaskState` before
its first stage. Every stage that receives a HASHI Tool Registry is wrapped by
the cognitive-control boundary, including Direct, tool-enabled
Strategy/Triage, Planning, Execution, Replanning, Review, and delegated or
sub-agent execution. Tool-free stages receive the same `TaskState` projection
so stage transitions cannot discard established progress.

The removed `cognitive_control_enabled` configuration field is invalid whether
its value is true or false. Rejecting it is intentional: silently ignoring a
legacy false value would let an operator believe that a required safety
mechanism had been disabled, while accepting true would preserve a misleading
optional contract.

The live Python hot-reload boundary may receive that retired keyword once from
an already-instantiated pre-migration adapter. A non-advertised compatibility
membrane accepts and discards only that stale internal argument so HASHI1 can
adopt the invariant without restarting unrelated active Agents. It does not
create state, alter behaviour, or make the configuration field valid; every
new provider is controlled unconditionally. Unknown constructor options remain
fatal.

## Safety objective

HER v2 must not permit a model to remain indefinitely in a no-new-information
reasoning/action loop merely because it can keep issuing syntactically
different or periodically repeated tool calls. The Runtime therefore tracks
observable tool actions, results, state changes, and evidence-linked TaskState
progress without storing or reconstructing hidden chain-of-thought.

After three identical semantic action/result cycles with no positive state
change, ordinary tools are replaced temporarily by the typed
`hashi_cognitive_decision` boundary. The active model must choose `FINALIZE`,
`REVISE_DIRECTION`, or `BLOCKED`. A revised direction must identify a genuinely
different focus, expected state change, explicit stop condition, and a narrow
subset of already-authorised tools. Returning to the same progress basin
becomes `NO_MEANINGFUL_PROGRESS`, after which another revision is not accepted.

## Boundaries

This mechanism is not a tool-call, token, elapsed-time, stage, or reasoning
ceiling. It does not reduce user-granted authority, grant new tools, fork a
second model judge, or replace `/stop`, cancellation, provider recovery,
transport inactivity protection, or the meaningful-progress idle detector.
Polling-only cycles remain exempt because observing an unchanged external job
can be legitimate progress evidence.

The invariant belongs inside HER v2, not in frontends or individual provider
adapters. Non-HER Engines keep their own lifecycle and safety contracts.

## Enforcement and acceptance

The decision is enforced at three non-optional boundaries:

1. configuration rejects the retired switch;
2. Runtime always creates the Turn-scoped `TaskState`; and
3. the stage provider always wraps every available HER v2 Tool Registry with
   cognitive control before exposing it to the model.

Acceptance requires regression tests proving all three boundaries, coverage of
every tool-enabled lifecycle stage and delegated execution, correct cycle and
TaskState-stagnation handling, unchanged-result polling exemption, restricted
direction revision, repeated-basin termination, and audit payloads that contain
only typed conclusions and observable evidence.

Any future proposal to make this mechanism optional requires a new explicit
user decision that supersedes this record and a design showing an equally
strong deterministic defence against infinite reasoning/tool loops. It must
not be introduced as a compatibility flag, experiment, provider option, or
silent default change.
