# Debug Call restart reconciliation — 2026-10-11

Status: accepted on HASHI3 from `debug-call`; offline restart checks, repeated
live restart acceptance, restart presentation, and the post-restart Call smoke
test pass

Owner: PAO for Function generation and reboot receipts; Frontend Connector for
Workbench restart presentation

Layer: replaceable Functions and external Workbench

Protected Core: unchanged

## Trigger

After the first clean Call consolidation, live HASHI3 exposed restart regressions:

- a first changed-source restart took about 80.0 seconds and an unchanged repeat
  took about 36.5 seconds;
- the completion banner could return after it had dismissed;
- one committed restart logged a Windows access-denied error while replacing the
  receipt journal, although a later terminal write completed;
- after the first cache recovery and a 12.2-second accepted run, a later live
  operation took about 37.0 seconds, proving that the current worker path still
  bypassed part of the recovered optimization.

The audit therefore compared every retained local and remote-tracking repair
line, current semantics, tracked release-worktree changes, and the code actually
used by the running shared Function.

## Branch classification

| Source | Classification | Action |
| --- | --- | --- |
| Recent October Workbench Call, history, desktop, nightly and visual branches | Ancestor or patch-equivalent | Keep current `debug-call` |
| `migration-hashi2/preserve/h2-runtime-cache-20260917` | Two formal cache commits were absent | Port the valid cache semantics and tests onto the current API |
| `migration-hashi1/preserve/h1-reboot-20260917` commit `c00f8824` | Superseded by the current locked-dependency comparison | Do not reapply |
| Historical worker-startup repair `02308cb1` | Already an ancestor | No action |
| Workbench `origin/fix/status-path-duration-drafts-20260917` | Documentation-only tip; implementation semantics already present in newer code | No merge |
| `migration-hashi2/exp-herv2j` commit `219eca64` | Old notice simplification; current code already removes the delayed-result prefix and uses newer concise notices with duration | Do not merge the experiment |
| `migration-hashi2/exp-herv2j` commit `4a21f972` | HERV2J `/stop` Worker-release experiment, not reboot lifecycle implementation | Outside this baseline; do not merge |
| `fix/nightly-nonvoice-20261003` | Obsolete divergent Call line | Do not merge; retain only behavior proven by the current Call suite |
| Historical HASHI2 cloud speech experiments | Explicitly retired | Exclude |

The HASHI2 cache branch is not merged wholesale. Its tip also contains an older
dependency-drift implementation. The current Function contract has a newer,
stricter form that validates the exact standard lock while tolerating unrelated
optional distributions.

The completed audit found no further formal branch to merge:

- backend restart optimization `2e4a923c` and every later October commit that
  touches Function generation are ancestors of `debug-call`;
- Workbench restart fixes `716fae7`, `4aa68ad`, `55c1105`, `ce37d6d`,
  `dea855e`, `87d5463`, `9618017`, `13715d2`, `ea67957`, `3de8e98`,
  `c1b99c6`, and `b63c20f` are ancestors of `debug-call`;
- Workbench `main` commit `de235f0` is patch-equivalent to current
  `a3b82f3` for exact Session/Run cancellation and has since evolved further;
- the divergent rolling-caption and immediate-media commits retain their valid
  tests and behavior in evolved current files, while their retired assertions
  and superseded decision notes remain excluded;
- current reboot notice tests already require no “补发” prefix, and current
  locale text is a newer concise start/success form with measured duration;
- the reviewed October release worktrees contain no additional tracked restart
  implementation that belongs in this baseline;
- older HASHI2 Call and cloud-speech lines conflict with explicit current
  requirements and remain excluded.

## Causes

1. The first branch audit concentrated on recent local and `origin` refs. The
   retained `migration-hashi2/*` remote-tracking namespace was not included, so
   two non-ancestor performance commits were incorrectly called equivalent.
2. Deployment verification initially treated source, built generation, and the
   running shared Function as one fact. Agent-scoped `/reboot` replaced Agent
   Workers but left the old shared Function process running.
3. The faithful cache port did not cover two newer API call paths:
   `qualified_generation()` verified the source and immediately handed it to
   `prepare_generation()`, which verified the same source again; then
   `prepare_worker()` called the uncached dependency digest directly.
4. Correctness suites covered source identity and fail-closed checks, but did not
   assert the trusted verified handoff or the worker's use of the current
   runtime-fingerprint cache.
5. The Workbench dismissal check covered the normal active-operation case, but
   not a late timer after a newer operation became the polling target.
6. Receipt persistence failed closed correctly, but transient Windows atomic
   replace denial had no bounded retry.

The later 37-second run was therefore not evidence of another missing branch.
It exposed an integration gap between the recovered cache and the current
Function-generation API.

## Repair

- Runtime verification uses a process-local fingerprint cache. A hit is allowed
  only while runtime policy and lock, protected Core source, and installed
  distribution metadata still match.
- `prepare_generation()` may skip its initial source check only for the
  immediate generation already returned by `qualified_generation()`. The
  default and every untrusted caller still verify source.
- The final pre-cutover source verification remains in place and unchanged.
- Worker preparation obtains the dependency digest from the authoritative
  current fingerprint cache instead of directly rescanning installed
  distributions.
- Receipt persistence retries only transient Windows access-denied or
  sharing-violation failures from the pre-commit atomic replacement, for at most
  0.8 seconds. Conflicts, permanent failures, other I/O errors, and post-commit
  durability failures remain visible.
- Workbench records a late-timer dismissal in the active connection scope even
  if a newer restart has replaced the polling target. The old timer cannot clear
  the newer operation, and polling cannot resurrect the older receipt.
- Live adoption checks source commit, built artifact, running shared Function
  generation, exact Agent target, message order, elapsed time, dismissal, and
  late polling as separate facts.

## Automated and focused verification

The current-API integration defect was recorded red before implementation:

- trusted verified handoff still repeated source verification;
- untrusted/default preparation still had to retain verification;
- worker preparation bypassed the runtime-fingerprint cache.

All three focused checks pass after the repair. The expanded Function-generation
and reboot suite passes 129/129, including source/failure boundaries, worker
quiesce, receipt persistence, command projection, adoption bridge, and cache
behavior. Compilation, whitespace, and protected-Core checks pass.

A local timing probe measured the direct dependency scan at about
0.031–0.034 seconds, the first cached fingerprint at about 0.093 seconds, and
unchanged cache hits at about 0.007–0.008 seconds. These probes isolate the cache
path; live restart timing remains the release evidence.

## HASHI3 live adoption and restart evidence

The earliest runs exposed an adoption gap rather than validating the fix:

| Operation | Exact target | Running source/generation | Result |
| --- | --- | --- | --- |
| `f706c352655344aa9b969fa75d66a5d0` | Lumi | old shared Function | about 80.0 seconds |
| `0bc235fb23f9406c84bfc8cfebc69461` | Lumi | old shared Function | about 36.5 seconds |

After the first cache adoption, operation
`2a1f42043a5b4e39951959fb0c56b561` completed for Lumi in 12.2 seconds with
8.6 seconds preparing. A later operation
`55f262…` took about 37.0 seconds and exposed the two current-API bypasses
described above.

HASHI3 was then started from the exact `debug-call` source commit
`d1a872c8bd2e7fcb43ea9f4ba5a8b1777372efc0`. Its running shared Function PID
is 62516 and its generation is
`sha256:0a8a2ae3e29a17b9a9a5d1e1202b87f13ecf97cae74b76f7dd02d54f3aab0fd9`.
All 11 Agents returned online.

Three real Workbench restart operations against that candidate completed:

| Operation | Exact target | Duration | Preparing |
| --- | --- | ---: | ---: |
| `36a529e7…` | Lumi (`agent1`) | 17.4 seconds | 13.92 seconds |
| `7962bcec…` | Lumi (`agent1`) | 14.3 seconds | 10.95 seconds |
| `eaf61d31…` | Lumi (`agent1`) | 9.8 seconds | 6.53 seconds |

The first was the cold run after the whole HASHI3 instance started. Each receipt
was committed, terminal, online, and carried an empty failure reason. In the
browser DOM, the canonical restart-start message preceded the matching success
message.

A MutationObserver installed before the third click recorded the presentation
lifecycle:

- no banner at 2 ms;
- progress banner at 5,669 ms;
- completion banner at 15,087 ms;
- no banner at 19,109 ms.

The completion state therefore remained for about 4.022 seconds and then
dismissed automatically. It was still absent after another 6.5 seconds of
polling, so the terminal receipt did not resurrect it.

## Post-restart Call acceptance

After the restart gate, the real Electron Workbench used the fixed Phone entry
against HASHI3. It opened the same direct video-first Call with the physical
camera preview, live microphone, and the natural opening “测试官，您好。露米在，
您请说。” There was no connected-state ringback.

During Call `5ece8928-5872-4111-9c07-b1afa6393dd1`:

- backend diagnostics recorded `call_started`, `opening_started`, camera
  enabled, and `opening_completed`;
- camera off then on produced the expected disabled/enabled transitions in the
  same Call;
- microphone mute then unmute updated the same visible Call controls;
- hangup recorded `call_ended` with `explicit_end`, disabled the camera,
  closed the panel, released media, and restored both fixed launch entries.

This completes the post-restart Call smoke gate. Camera/device refusal remains
an allowed degradation to audio-only; it is not treated as a failed Call start.

HASHI4 was not rebooted.

## Shared-replacement cache follow-up

A later Call correction required a new HASHI3 Function generation and exposed
one more distinct restart path. Operation 23e386ff… spent about 106.6 seconds
preparing. The immediate unchanged-source operation e22ab3db… still spent
about 87.6 seconds preparing. Their Workbench start/success ordering, four-second
completion dismissal, non-resurrection, restoration, and terminal receipts were
correct, but their timing failed the release gate.

The second operation selected the same
sha256:eef3850c4419fdf01b4e1e517f4141f9be61ee84b89b9445f499e64dc19d9842
generation and materialized its existing artifact in about 0.97 seconds. It
still spent about 38.2 seconds qualifying: initial manifest 8.9 seconds,
isolated probe 15.6 seconds, and final manifest verification 12.4 seconds.

This was not another missing branch. The earlier repair covered the live
FunctionWorkerSupervisor cache path. Shared Function replacement first runs
runtime_release.qualify_release() in a separate pre-start process, and that
path always called the full probe before persisting the same cache again. It
never attempted the qualified-cache read used by the supervisor.

The release process now checks the persisted qualified generation first. Reuse
is allowed only when the cache revalidates the exact current runtime, checkout
bytes, immutable artifact, and configured observer set. A miss or observer
change still runs the complete isolated qualification, materialization,
persistence, and final commit checks. A focused regression recorded the former
unconditional probe red and now passes. Consecutive live timing remains pending
until this exact committed candidate is adopted.

## Rule for future integrations

Future recovery branches must enumerate every local, remote-tracking, reflog, and
release-worktree source; classify patch-unique commits by current semantics; and
explicitly reject obsolete lines. Branch ancestry alone is insufficient.

Every restart release must also prove:

1. no duplicate qualification at a trusted handoff;
2. workers use the current runtime-fingerprint cache;
3. the final pre-cutover source verification remains;
4. source, artifact, service, shared generation, renderer, receipt, and live
   observation identities agree;
5. at least two consecutive real restarts meet timing, ordering, restoration,
   dismissal, and non-resurrection gates;
6. the post-restart Call smoke test passes before deployment is called complete.
