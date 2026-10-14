# Fast, reliable and truthful hot reboot

## Scope and authority

On 2026-10-14 the user authorized implementation and operational testing only
on HASHI3 branch `debug-reboot`. HASHI4, `/restart`, Remote and Protected Core
are outside scope. PAO owns the lifecycle and receipts; Frontend Connector
Functions own localized result wording. The user required incorrect assertions
to be corrected and observed red before implementation.

## Implemented correction

- Replacement identity is Agent + expected Worker nonce + target generation,
  backed by the Supervisor route and process handle. PID and timestamps remain
  diagnostic, so Windows PID reuse cannot manufacture failure.
- Receipts use canonical outcome plus independent observation confidence and
  retain one bounded evidence record per target. Partial and unconfirmed results
  name only affected Agents and their failed checks. Non-disruptive post-commit
  warnings state the actual failed follow-up step.
- Targeted reboot prepares candidates while old routes remain online, checks
  activity a second time, then fences only the exact targets for drain and
  atomic pointer commit. Broad reboot no longer fences routes before publishing
  the existing Core handoff request.
- A qualified generation cache hit uses a Git/runtime seal. A new generation
  performs one isolated full-closure probe and one artifact materialization.
  Function Worker Host no longer repeats two artifact scans and a complete
  import/contract validation already owned by the probe. The protected
  bootstrap verification and real per-Worker backend startup remain.
- Receipts record stage timings and counts for candidate Workers, manifest
  passes and full-closure imports. These diagnostics do not lengthen user
  messages.
- Scheduler behavior was not replaced: old Functions continues running during
  preparation, and the existing durable startup recovery emits one due
  heartbeat once with recovery identity after the short handoff.

## Red/green and offline evidence

The tests-first checkpoint is `26ca3642`. Before correction, focused cases
failed on PID inequality, nonce mismatch being hidden by PID change, premature
broad fencing, duplicate generation/Worker validation, missing two-axis result,
and loss of failed-Agent detail. Additional review cases failed on a remaining
legacy cutover full scan, hard-coded work counts, ambiguous 19/20 messaging and
terminal recovery records without an outcome.

Implementation checkpoint `6021be54` then passed:

- 137/137 directly affected reboot, Worker, receipt, UI and Scheduler tests;
- 219/219 clean-checkout focused lifecycle, generation, runtime, protocol,
  adoption and release tests;
- project Ruff gate for every changed Python file;
- Python compilation and both locale JSON parses;
- `scripts/check_protected_core_changes.py` with no Core change.

These are offline implementation facts, not running adoption or user-visible
delivery evidence.

## Live HASHI3 verification

Pending. Record targeted and broad hot-reboot receipts, stage timings, Core and
Remote invariants, heartbeat behavior and user-facing start/final messages here
after the authorized HASHI3-only runs. HASHI4 must remain untouched.
