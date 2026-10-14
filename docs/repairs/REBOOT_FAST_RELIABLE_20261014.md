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

Authorized HASHI3-only adoption completed on 2026-10-14. The first broad reboot
was an adoption run initiated by the old Functions generation, so its 62.8 s
duration is recorded but is not used as the corrected-path benchmark. It loaded
source commit `731592adfe9afd6e0a1716bc99ef527b9bb2aaef` as generation
`sha256:edd9fde514e6aa27c0879cdf936ec35518298b5df2da0d1c39644ea91c86b2ad`.

Observed pre-follow-up lifecycle evidence:

Evidence correction: the five 4.6-5.5 s `min` runs and two 20.6-20.7 s
`max` runs below were submitted through the legacy Workbench administration
endpoint. That endpoint reached the same RebootManager but called PAO directly
instead of entering through the standard Frontend Connector slash-command RPC.
The timings remain valid process-replacement evidence, but they are not
end-to-end FC latency or frontend-parity evidence; the earlier 5.4 s p95 claim
is withdrawn. Real Telegram runs of 44.8 s and 37.7 s for `min`, followed by
49.5 s for `max`, exposed a qualification-cache provenance loop.

The follow-up repair sends the Workbench administration action through the same
`/reboot min` FC-to-Worker command path, removes its duplicate activity query,
and leaves PAO's one authoritative preflight intact. A clean new Git commit with
identical Function bytes now performs one exact byte revalidation and refreshes
the qualification receipt without an isolated probe. Immutable artifact
creation provenance remains historical and can no longer overwrite the current
qualification commit during a broad handoff. Changed Function bytes still fail
closed. Fresh end-to-end FC results are recorded only after committed-source
adoption.

- five same-generation `min` runs completed in 5.5, 5.2, 4.6, 4.9 and 5.0 s;
  every receipt was `succeeded` + `confirmed`, with no warnings. These are
  retained as RebootManager/Worker measurements only;
- same-generation `min` recorded one candidate Worker, zero full-closure imports
  and zero qualification work. After the first warm-up seal, each run recorded
  one necessary manifest hash rather than one full generation scan;
- RebootManager-level `max` runs completed in 20.6 and 20.7 s with 11/11 Workers
  online. Qualification, materialization and full-closure import counts were
  zero; each of the 11 new process bootstraps verified its own artifact once;
- during the instrumented `max`, Workbench/API unavailability was 0.83 s. The
  start notice was visible before the disconnect and the final message reported
  an unambiguous success, elapsed time and 11 online Agents;
- Core PID 12824 and the two Windows launcher/child Remote process identities
  (19984 and 20204) remained unchanged. Shared Functions and Worker identities
  changed, as required;
- a temporary, initially disabled heartbeat was enabled only for boundary
  acceptance. Preparation-time occurrences completed normally. The occurrence
  due across handoff completed exactly once after recovery, with no pending
  recovery batch, and the job was disabled before closeout. It remained disabled
  for more than one Scheduler interval with no additional run;
- deterministic candidate rejection, drain failure, atomic group rollback and
  second activity-gate cases passed 4/4 after live adoption. No production fault
  injection switch or test backdoor was added merely to manufacture a live
  failure.

The HASHI3 topology has 11 Workers, not the production topology's 26. The prior
live result proves the RebootManager lifecycle and 11-Worker process timings,
not the follow-up FC entry correction and not a 26-Worker p95 sample. The
offline count regression still proves that full-closure work is not multiplied
by Worker count. HASHI4 was neither modified nor rebooted.
