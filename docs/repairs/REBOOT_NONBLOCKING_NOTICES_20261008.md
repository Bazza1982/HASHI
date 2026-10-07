# Reboot without presentation dependencies

## Decision and scope

The user requested immediate repair and validation in HASHI3 on 2026-10-08.
PAO owns the Functions lifecycle and receipt journal; optional Persona wording
uses its existing Functions owner. No Core migration or production HASHI4 change
is authorized by this repair. Previously authorized HASHI3 operational testing
is recorded in the nightly repair decision; source, offline qualification and
real adoption evidence remain separate.

Durably accepted reboot requests must proceed independently of browser focus,
display ACKs or notice transport availability. A visible chat message and a
banner ACK are distinct facts. Neither supplies operational authorization.

## Implementation

- New operations request no browser presentation ACK. Legacy schema-5 pending
  ACKs remain readable and authenticated, but do not gate execution or move a
  later lifecycle phase back to announcing.
- The initial notice attempt is bounded to two seconds. Failure is recorded
  separately; the authoritative operation proceeds. Final notices retain the
  persisted four-attempt retry budget in an independent watcher. The lifecycle
  loop never waits for that outbox to drain.
- Quiesce pauses/cancels optional Persona status tasks. An epoch invalidates a
  late Provider response even after rollback resumes the Worker. Actual Runs,
  queues and background tasks retain their drain checks.
- Bounded receipt retention expires the oldest terminal record, including an
  undelivered notice, before refusing a new operation. Such expiration is logged
  and never represented as sent. Recent outcomes/retries retain their evidence;
  accepted/running operations are never evicted. The existing 50-record/128-KiB
  retention and retained-request deduplication window remain bounded.

Invalid/unwritable journals, real active work, scope/authorization checks,
committed-source and runtime compatibility, independent qualification, READY,
atomic route commit and verified recovery remain valid transaction boundaries.

## Offline verification

The focused pre-fix run failed eight behavioral cases and passed the genuine
background-work control. It exercised actual RebootManager handoff publication,
route switching, RebootReceipts persistence/capacity and FunctionWorkerHost drain
with isolated process/transport fixtures, never an operational restart.
The initial repaired owning suite passed 81 cases, including late noncooperative
Persona rendering, authenticated legacy observations and notification recovery
without rerunning a reboot. The wider lifecycle suite passed 211 cases; three
real source-qualification cases require committed source and will be rerun
after the repair commit. Further gate results are recorded after execution.

Private evidence is under the instance's ignored `state/reboot-fix-20261008/`.
The first scratch invocation had a missing temporary parent directory and is
not the red evidence; `red.log` is the corrected eight-defect reproduction.

## Live verification

Pending. A source/test pass is not proof of active shared Functions adoption.
