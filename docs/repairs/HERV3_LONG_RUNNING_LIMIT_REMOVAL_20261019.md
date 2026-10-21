# HERV3 long-running limit removal — 2026-10-19

Status: source implemented and offline verified on HASHI3 branch
`debug-issues-oct`. No Worker restart or live acceptance is part of this
change. Functional owner: HERV3. Engineering layer: Functions.

## Decision

HERV3 must support long-running agentic work. It has no HASHI total token,
message, Turn, model-call, Tool-call, elapsed-time, output, continuation-history,
receipt, WIP, task-state or completed-Session-history ceiling. Privacy level is
not an execution budget and cannot reduce loop size.

Raising old constants is not an acceptable fix. Complete state is retained;
large data is segmented or paged where a storage or process boundary needs
frames. Every segment remains ordered, integrity checked and recoverable.

## Removed ceilings

- Provider continuation: removed the 512-message and 8 MiB aggregate caps.
  Messages now use an ordered segmented store with item and aggregate digest
  verification, legacy-inline compatibility and lossless steer inheritance.
- Recovery: removed the 256 receipt/side-effect cap and all fixed prior-turn,
  WIP-ID, evidence-reference and response-ID tail windows.
- Privacy Level 2: removed the 500,000-character request and 2 MiB aggregate
  rejection. Detector input is internally paged with overlap and merged spans;
  privacy policy is unchanged.
- Task state and WIP: removed goal, text, identifier, item, history, record,
  row, file, capsule, excerpt, depth and list truncation/eviction.
- Checkpoints and settled history: removed fixed receipt counts, argument-key
  counts, metadata/result excerpts, settled-turn counts and prompt text tails.
- Provider parsing and repair: removed structured-input/candidate caps and the
  DeepSeek invalid-tool repair text cut.
- Provider continuation and fallback: removed inherited output-token settings,
  the one-physical-call fallback target cap and fixed fallback-state caches.
  Consent/cost policy still controls route handovers; once selected, a target
  receives the normal continuous HERV3 model/tool loop.
- Presentation and delivery state: removed commentary, required-message,
  change-reason, next-step, pending commentary, delivery-receipt and accounting
  cache truncation/eviction that could discard long-run evidence.

Tool APIs may page one response with an offset or cursor, and external Providers
may impose official request/media constraints. Paging preserves access to all
authorised source data and does not cap the HERV3 Turn.

## Retained non-execution boundaries

The following are deliberately retained because none limits healthy total work:

- explicit user cancellation;
- the meaningful-progress idle detector, reset by real progress;
- narrow Provider connection/read/stream inactivity and protocol guards;
- an explicitly requested timeout for one Tool invocation;
- official external Provider media/body constraints;
- retry and replay-safety decisions after a typed failure;
- small rolling windows used only for cycle detection, rate limiting or
  non-authoritative log projection;
- effort/cadence controls for optional review or advisory checks.

Canonical durable state is not the bounded audit/log projection. A liveness or
transport guard may end a genuinely stalled operation; it may not expire or
truncate an otherwise healthy long-running task.

## Verification contract

Regression coverage crosses every removed historical boundary and asserts full
round-trip content, not merely acceptance of a larger constant. The focused red
baseline reproduced eight failures covering Provider context, recovery receipts,
Privacy Level 2, task state, WIP, settled history, fallback continuation and
output-token inheritance. Green verification must include those cases, the
broader HERV3/provider/presentation suites, syntax, whitespace and protected-Core
checks.

Source, commit, Worker adoption and live acceptance are separate facts. The user
will perform restart and continued live testing after this source checkpoint.

## Verification record

- Historical-boundary and directly affected suites: 445 passed, 2 skipped.
- Curated deterministic Core gate against committed Function source: 861
  passed, 1 skipped.
- Edited production modules compiled; `git diff --check` passed.
- `scripts/check_protected_core_changes.py` passed; Protected Core is
  unchanged.

The skips are existing platform/conditional cases, not suppressed failures.
