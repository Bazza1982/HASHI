# Debug Call restart reconciliation — 2026-10-11

Status: candidate on `debug-call`; offline checks pass, live HASHI3 adoption pending

Owner: PAO for Function generation and reboot receipts; Frontend Connector for
Workbench restart presentation

Layer: replaceable Functions and external Workbench

Protected Core: unchanged

## Trigger

After the first clean Call consolidation, live HASHI3 showed three restart
regressions:

- a first changed-source restart took about 80.7 seconds and a repeated restart
  took about 25.4 seconds;
- the completion banner could return after it had dismissed;
- one committed restart logged a Windows access-denied error while replacing the
  receipt journal, although a later terminal write completed.

The Call repair itself was not the only relevant lineage. The audit therefore
expanded from recent local/origin branches to every retained remote-tracking
repair ref and compared ancestry, patch identity, and current semantics.

## Branch classification

| Source | Classification | Action |
| --- | --- | --- |
| Recent October Workbench Call, history, desktop, nightly and visual branches | Ancestor or patch-equivalent | Keep current `debug-call` |
| `migration-hashi2/preserve/h2-runtime-cache-20260917` | Two formal cache commits were absent | Port the cache and its tests onto the current API |
| `migration-hashi1/preserve/h1-reboot-20260917` commit `c00f8824` | Superseded by current locked-dependency comparison | Do not reapply |
| Historical worker-startup repair `02308cb1` | Already an ancestor | No action |
| Workbench `origin/fix/status-path-duration-drafts-20260917` | One docs-only tip; implementation semantics already present in newer code | No merge |
| `fix/nightly-nonvoice-20261003` | Obsolete divergent Call line | Do not merge; retain only behavior proven by the current Call suite |
| Historical HASHI2 cloud speech experiments | Explicitly retired | Exclude |

The HASHI2 cache branch is not merged wholesale. Its tip also contains an older
dependency-drift implementation; the current Function contract has a newer,
stricter form that validates the exact standard lock while tolerating unrelated
optional distributions.

## Causes

1. The earlier branch audit concentrated on recent local and `origin` refs.
   The retained `migration-hashi2/*` remote-tracking namespace was not included,
   so two non-ancestor performance commits were incorrectly called equivalent.
2. Correctness suites covered source identity and fail-closed runtime checks but
   did not make two consecutive live restart timings a release gate.
3. The Workbench dismissal check covered the normal case where the dismissed
   operation was still the active polling target. It did not cover a late timer
   after a newer operation had become the target.
4. The receipt journal correctly failed closed on disk errors, but Windows can
   transiently deny the atomic replace. That pre-commit condition had no
   bounded retry or focused test.

## Repair

- Function-generation verification now uses a process-local runtime fingerprint
  cache. A hit is allowed only when the runtime policy and lock, protected Core
  source, and installed distribution metadata still match.
- The cache never changes runtime comparison behavior and exposes deterministic
  hit/miss counters for tests.
- Reboot receipt persistence retries only Windows access-denied/sharing-violation
  failures from the pre-commit atomic replacement, for at most 0.8 seconds.
  Conflicts, permanent permission errors, other I/O failures, and post-commit
  durability failures are not retried or hidden.
- Workbench remembers an operation dismissed by a late timer in the active
  connection scope even if a newer restart has already replaced the polling
  target. The old timer cannot clear the newer operation, and later discovery
  cannot resurrect the older receipt.

## Verification before live adoption

- runtime fingerprint cache: 6/6 pass;
- reboot receipt retry and existing disk-failure boundaries: 4/4 pass;
- complete reboot manager suite: 70/70 pass;
- Workbench connection-transition integration: 11/11 pass, including the new
  late-dismissal reproduction;
- focused restart banner/policy checks: 22/22 pass before the added integration
  case;
- protected Core gate: pass;
- cache timing under the independent development interpreter: first calculation
  0.220 seconds, unchanged second calculation 0.027 seconds.

The complete Function-generation test file passes 15 cases under the available
independent Python 3.14 environment. Four cases correctly reject that interpreter
because production requires Python 3.12.13. They must be exercised by HASHI3's
supported live Function qualification; this mismatch is not waived.

## Live gate

Before this candidate can be called complete:

1. commit the exact `debug-call` source;
2. load it into HASHI3 only;
3. observe one changed-source restart and one unchanged-generation restart;
4. verify canonical start/completion messages and absence of receipt write
   errors;
5. verify the completion banner disappears after four elapsed seconds and does
   not return on subsequent polling or command entry;
6. run the post-restart Call smoke test;
7. keep HASHI4 running without reboot.

Future integrations must enumerate all local and remote-tracking refs, classify
patch-unique commits by current semantics, and record two consecutive live
restart timings. Passing a single restart or only unit tests is insufficient.
