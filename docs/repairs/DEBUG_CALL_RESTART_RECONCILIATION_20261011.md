# Debug Call restart reconciliation — 2026-10-11

Status: adopted on HASHI3 from `debug-call`; offline and live restart gates pass;
the post-restart Call smoke test remains separate

Owner: PAO for Function generation and reboot receipts; Frontend Connector for
Workbench restart presentation

Layer: replaceable Functions and external Workbench

Protected Core: unchanged

## Trigger

After the first clean Call consolidation, live HASHI3 showed three restart
regressions:

- a first changed-source restart took about 80.0 seconds and an unchanged
  repeat took about 36.5 seconds;
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

The final audit found no further formal branch to merge:

- backend restart optimization `2e4a923c` and every later October commit that
  touches Function generation are ancestors of `debug-call`;
- Workbench restart fixes `716fae7`, `4aa68ad`, `55c1105`, `ce37d6d`,
  `dea855e`, `87d5463`, `9618017`, `13715d2`, `ea67957`, `3de8e98`,
  `c1b99c6`, and `b63c20f` are ancestors of `debug-call`;
- recent Call branches are ancestors or patch-equivalent;
- older HASHI2 Call and cloud-speech lines conflict with explicit current
  requirements and remain excluded.

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
5. Most importantly, deployment verification treated source, built generation,
   and running shared Functions as if they were one fact. Agent-scoped
   `/reboot` replaced Agent Workers but left the old shared Functions process
   running. That old process still performed the expensive qualification path,
   so the new cache was present on disk but absent from the live path.

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
- Live adoption now has an explicit generation check. After a shared Function
  change, the running Function PID and generation must match the qualified
  candidate before restart timing can be accepted. An Agent Worker reboot alone
  is not adoption evidence.
- The live gate measures two consecutive operations and verifies the exact
  target, message order, elapsed time, dismissal, and late polling behavior.

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

## HASHI3 live adoption and restart evidence

The first two runs exposed the adoption gap rather than validating the fix:

| Operation | Target | Running shared Function | Result |
| --- | --- | --- | --- |
| `f706c352655344aa9b969fa75d66a5d0` | Lumi | old generation | about 80.0 seconds |
| `0bc235fb23f9406c84bfc8cfebc69461` | Lumi | old generation | about 36.5 seconds |

The live source already contained commit `f19c3da4`, but the shared Function
PID and generation were still the pre-fix values. The authorized HASHI3
Function replacement then adopted generation
`sha256:7a4278d783e38e980086e117f599180c38292f3cdbbf8af07d4635294cc8cb7c`
under PID 53440. Core PID 29144 did not change, and all 11 Agents returned
online.

Post-adoption evidence:

| Operation | Exact target | Duration | Qualification |
| --- | --- | --- | --- |
| `59e21395a6164b9fa4cc9464baadebee` | Portfolio Manager | about 14.8 seconds | performance evidence only; the UI coordinate selected the wrong Agent |
| `2a1f42043a5b4e39951959fb0c56b561` | Lumi (`agent1`) | 12.2 seconds; preparing 8.6 seconds | exact acceptance run |

For the exact Lumi run:

- the receipt is committed, terminal, online, and has an empty failure reason;
- the canonical “开始热重启…” message precedes
  “Lumi (Chief of Staff)热重启成功，耗时12.2秒。”;
- the top completion banner disappeared after its four-second display window;
- it remained absent after later polling while the terminal receipt was still
  discoverable, so there was no resurrection;
- HASHI4 was not rebooted.

The restart reconciliation is accepted. The remaining live gate for the larger
`debug-call` repair is the post-restart Call smoke test.

Future integrations must enumerate all local and remote-tracking refs, classify
patch-unique commits by current semantics, and record two consecutive live
restart timings. They must also compare the expected Function generation with
the running shared generation. Passing a single restart, only unit tests, or
only checking the live checkout is insufficient.
