# Remote idle CPU repair — 2026-10-05

Status: source checks and scoped live recovery passed; remaining observations
are explicit below.

## Approval, ownership and scope

The current user requested repair of the diagnosed idle CPU problems, a WSL
restart, disabling HASHI2 Exchange, and restarting all affected Remotes.
This authorizes the HASHI1/HASHI2 operational interruption and the HASHI2
configuration change for this task only. It is not Core major-version migration
authorization, scheduler catch-up authorization, or a global Exchange policy.

Functional owner: PAO for Remote receipt supervision and peer routing.
Engineering layer: replaceable Remote Functions; Windows/WSL launcher platform
configuration; HASHI2 instance configuration. Protected Core was unchanged.
The same seven-file implementation was applied to HASHI1–HASHI4. The deployed
Windows WSL runtime launcher adopted the packaged launcher correction.

## Implementation decisions

1. A persistent, bounded read-only receipt-discovery cache eliminates repeated
   parsing of unchanged Superloop history. It keys revisions by metadata,
   invalidates append/replacement/deletion/corruption, and shares loop evidence
   once per matching pass. Disabled controllers are excluded before their
   history is read. Locked admission still reloads canonical authority; retry
   deadlines, pause linearization, original Session/request identity, the
   single recovery budget and delivery reconciliation retain their contracts.
2. An authenticated successful known same-machine loopback candidate is a
   routing success, not evidence that the peer's advertised address is stale.
   Existing peer/profile ownership supplies that hint. Unknown real bootstrap
   fallback still registers normally. LAN discovery, peer identity validation,
   trust refresh and ordinary heartbeat timestamp updates remain enabled.
3. The hidden Windows WSL launcher supplies a validated empty regular file as
   stdin. It no longer inherits the problematic console input handle. Native
   exit-code ownership and stdout/stderr logging remain unchanged; existing
   nonempty or unsafe stdin paths fail rather than being overwritten.
4. HASHI2's existing Exchange document was changed only to enabled=false
   through revision-checked configuration persistence. Its registration,
   credential reference, endpoint and published-Agent fields were preserved.
   No credential values were reported. LAN Remote is independent of Exchange.

Implementation commits qualified before operational adoption:

| Instance | Branch | Implementation commit |
| --- | --- | --- |
| HASHI1 | feat/herv3-scoped-search | 2f1ce853f063dd58fe80c3929e9f7dbca8373330 |
| HASHI2 | experiment/call-hashi2-live-20261004 | 7a76c5f94b246c25237d07b3c1bfee484d39682a |
| HASHI3 | fix/nightly-nonvoice-20261003 | 895c8dc99d3561b3428fcfb444b9655af3a25730 |
| HASHI4 | main | 37e436964d744780c2a1620d0f82b98a03cb38c4 |

These are local scoped commits, not a merged release. Later documentation
commits record observations; they do not imply new running Worker adoption.

## Focused failure and source verification

The prior implementation failed real persisted-Store regressions for repeatedly
loading unrelated historical receipts, rereading disabled controllers, and
reloading unchanged queued history. Both known peer and profile same-host
route cases reproduced a redundant discovery before the correction. The
unknown-fallback positive case remains covered.

The repaired receipt suite passed 38 tests. The focused native Windows set
(receipts, peer status, WSL packaging/launcher) passed 148 tests with 2 platform
skips. Windows packaging tests execute the actual PowerShell launcher with
native fixture executables: stdin reaches EOF, stdout/stderr are retained,
native failure/exit 23 is propagated, and an existing nonempty input file is
preserved while launch is rejected. Source substring checks are not acceptance.

Final configured Core gates, using Python 3.12.13 and committed qualified source:

- Native Linux filesystem: 757 passed.
- Native Windows: 766 passed, 1 skipped.

Earlier runs over Windows-mounted Linux paths timed out, and pre-commit
qualification runs rejected uncommitted Function source. Those were not counted
as passing validation; final native-filesystem gates replaced them. An initial
global Windows Python 3.14 invocation was stopped and was not release evidence.
No running Core interpreter dependencies were installed or upgraded.

## Live restart and observation

At approximately 22:08 AEDT the two WSL Remote units and Windows user-runtime
launchers were stopped, WSL was shut down, and its configured user runtimes were
started. The WSL kernel boot identity changed, confirming a real restart.
Windows HASHI3/HASHI4 Remote controllers restarted only their exact Remote
process chains. Their Core lifecycle was not requested or exercised.

The actual new Remotes were HASHI1 PID 381, HASHI2 PID 382, HASHI3 PID 2908,
and HASHI4 PID 38896. All four /health responses were HTTP 200 / ready and
claimed the implementation commits above. Authenticated handshakes between the
four affected peers were accepted.

Measurements use process CPU-time deltas, not lifetime ps percentages.
Remote percentages below use one logical CPU = 100%; Windows whole-host
percentages divide by 24 logical processors. WSL has 8 logical processors.
Each window comprises three five-second samples with no validation suite running.

| Subject | Before, 22:06 | First after, 22:12–22:15 | Later, 22:18–22:19 |
| --- | --- | --- | --- |
| HASHI1 Remote, one-core CPU | 22.18–27.78%, mean 25.04% | 1.0–1.4%, mean 1.2% | 1.6–2.0% |
| HASHI2 Remote, one-core CPU | 9.79–10.79%, mean 10.46% | 1.4–2.2%, mean 1.73% | 1.6–1.8% |
| HASHI1 cached reads | mean 16.35 MiB/s | mean 0.35 MiB/s | 0.35–0.59 MiB/s |
| HASHI2 cached reads | mean 7.85 MiB/s | mean 0.37 MiB/s | 0.22–0.33 MiB/s |
| Old WSL runtime clients + their hot consoles, whole-host CPU | combined 6.10–9.60% | replacement clients/consoles measured 0.0% over 8 seconds | no busy client/console in later samples |
| Windows vmmemWSL, whole-host CPU | 2.43–3.42% | 1.29–1.53% | 1.14–2.17% |

The cached-read counters do not mean physical disk throughput: Linux physical
reads remained zero in these samples. Reduced repeated parsing, not faster
storage, accounts for this specific Remote improvement. Aggregate host CPU is
not a controlled experiment; unrelated Windows applications remain active.
The earlier user screenshot's 32.8% vmmem spike was not reproduced during this
repair and is not the comparable baseline.

At 22:15, HASHI1 and HASHI2 Backend APIs were ready/non-degraded with all
7 and 5 configured active Agents online, respectively; HASHI2 Arale's real
Worker was ACTIVE/accepting and Telegram ingress/outbound were connected.
HASHI4's already-running Backend API and all 26 active Agents remained ready.
WSL Remote units, the configured Cascade worker, cron, PostgreSQL and xrdp
were active after recovery. This is not a new cross-frontend live test or
a complete end-to-end HChat acceptance claim.

Bounded actual Remote log tails through 22:16 showed zero repeated
Handshake: switched events on HASHI1/HASHI2 and no HASHI2 Exchange retry.
HASHI3/HASHI4 each made one valid startup repair from an unusable candidate to
the working Windows LAN address; these are not the removed repetitive
same-host loopback rewrites. Ordinary registry heartbeat updates still occur.

## Remaining boundaries

- The inherited console handle was demonstrably signaled while its input queue
  was empty; a newly opened control handle timed out normally. WSL's console
  relay then spins on the inherited handle. The EOF launcher avoids that faulty
  path and live CPU improved. The exact upstream Windows/WSL event that first
  corrupted the inherited handle is not proven.
- HASHI3/HASHI4 Exchange remains enabled and still logs CONNECTION_FAILED
  approximately every 30 seconds. The user named HASHI2 for disabling Exchange;
  that was not widened to the other instances. This remaining connectivity
  issue is not reported repaired and is not the measured WSL idle-CPU source.
- HASHI3's main runtime was not listening at the post-restart Backend API
  probe, while its Remote was ready. Its existing user-runtime task was not
  running. No claim is made that HASHI3 Core was recovered, or that this task
  established when/why it stopped; its Core was not restarted.
- Source tests, actual adopted Remote code, runtime configuration and measured
  improvement are separate evidence. Short observations cannot establish that
  every future load spike or optional WSL service is healthy.
