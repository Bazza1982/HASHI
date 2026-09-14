# Nightly repair: critical HER and timezone Function fixes

Started: 2026-09-14
Completed: 2026-09-15 (Australia/Sydney)

Baseline: `f22e87abbcef1abb60933671d3579c25d210719d`

## Scope and ownership

This repair contains only the three approved high-priority items:

- HER v2 owns execution-owner leases, interrupted-Turn reconciliation, and
  late terminal receipt repair in the Function Worker layer;
- PCM owns host-independent conversation timeline ordering in the Function
  layer; and
- PAO Scheduler owns persisted IANA timezone semantics, delayed timestamps,
  cron/DST resolution, recovery, and user-facing schedule rendering in the
  shared Functions layer.

No protected Core source, dependency, instance identity, credential, or user
data is part of the change.

## Result

HER v2 now records a stable Worker execution owner and process lease. Candidate
initialization leaves a Turn alone when its recorded owner is the same Worker,
is still alive, or cannot safely be proven dead. Reconciliation is limited to
requests whose different owner is confirmed dead. A late terminal success or
failure receipt re-derives unresolved side effects and clears stale
`UNKNOWN_SIDE_EFFECT` state without discarding a real failure audit record.

PCM now converts absolute timestamps through an explicit timezone policy and
orders them in UTC. A naive legacy timestamp is accepted only when its source
IANA timezone is known; otherwise it is rejected instead of inheriting the host
timezone.

Scheduler jobs now persist an IANA timezone. Cron evaluation, missed-run
recovery, Dream migration, delay persistence, and rendering use that timezone
explicitly. Ambiguous fall-back times choose the first occurrence by default;
nonexistent spring-forward wall times advance to the first valid minute. Delay
records retain UTC instants plus their source timezone. Merely reading a legacy
job does not create an unsolicited configuration write.

## Focused red/green evidence

Before the fixes, focused regressions exposed three boundaries: a candidate
could reconcile a live owner Turn, a naive PCM timestamp changed meaning with
the host timezone, and Scheduler recovery/rendering relied on an implicit
fixed offset. The new HER owner tests initially failed at the missing owner
contract, while the timezone tests failed under a non-local host timezone.

After the fixes:

- HASHI2 Linux/WSL: 155 focused tests passed; the five host-independence nodes
  also passed with `TZ=Australia/Sydney`;
- HASHI3 native Windows: 152 passed and three platform-specific tests skipped;
- HASHI3 WSL environment: 11 exact new owner/timezone nodes passed;
- Ruff checks, Python bytecode compilation, `git diff --check`, and protected
  Core checks passed; and
- the 13 runtime source files embedded in each adopted Worker generation match
  the reviewed source byte-for-byte by SHA-256.

One deliberately broader PCM module run reached 59 passes and one pre-existing,
out-of-scope assertion for the retired `PROVIDER_UNKNOWN` wording. It is not in
the focused acceptance set and this repair does not change that unrelated HER
error-code contract.

## Live Function adoption

Only Agent-scoped `/reboot min` operations were used. No `/restart`, Core
restart, or shared Functions replacement occurred.

- HASHI2 operation `fb14ea2bdbdc44febdff0037ad05a956` succeeded for `arale`.
  Worker PID `977534` / generation `a4d07e97...` was replaced by PID `1215665`
  / generation `35e1ec9c...`; Core PID `771764` and shared Functions PID
  `772839` / generation `9791e97f...` remained unchanged.
- HASHI3 operation `f05b64655e02433181f3c8dc0e7f7494` succeeded for `agent1`.
  Worker PID `32112` / generation `d71bc51d...` was replaced by PID `29668` /
  generation `d9d612a9...`; Core PID `3216` and shared Functions PID `27240` /
  generation `2790e9c4...` remained unchanged.

The HER and PCM Worker code is therefore live on both canaries. The Scheduler
source is fixed and independently green on both operating systems, but its
already-running shared Functions process was intentionally not replaced. Live
Scheduler adoption remains a separate broad handoff and is not claimed by an
Agent `/reboot` receipt.
