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
The repaired owning suite passed 81 cases, including late noncooperative Persona
rendering, authenticated legacy observations and notification recovery without
rerunning a reboot. Disabling epoch invalidation in an isolated in-memory module
made the late-render regression fail; the real source passed without mutation.
The wider lifecycle suite passed 211 cases before commit, then the three real
source-qualification cases passed against committed repair source `b00edeae`.
The curated Core gate passed 807 cases, with one POSIX-only case skipped on
Windows. Ruff, whitespace and protected Core checks passed.

Private evidence is under the instance's ignored `state/reboot-fix-20261008/`.
The first scratch invocation had a missing temporary parent directory and is
not the red evidence; `red.log` is the corrected eight-defect reproduction.

## Live verification

HASHI3 hot-adopted Functions source `b00edeae` and generation
`sha256:2041a71bed30c9b6bd4f7203251de467a8a7477935c0162f40486a2acc66e7bc`.
The first adoption used an isolated test browser because the old coordinator
still required a rendered start ACK. Its durable receipt completed successfully
in 69 seconds; shared Functions and all eleven Agent Worker PIDs changed.

After closing that browser, one ordinary authenticated `/reboot max` request
was submitted through the existing HASHI3-only Connector, without launching a
browser or posting a presentation ACK. Its authoritative receipt completed
successfully in 51.8 seconds with `presentation_ack.status=not_required`.
Shared Functions and all eleven Workers again changed PID, old processes exited,
and the same qualified generation reported all eleven exact Agents ACTIVE,
accepting and online. Core PID 40696 and its runtime/source fingerprint remained
unchanged across both operations. No HASHI4 or external frontend source changed.

Private evidence: `adoption.json`, `adoption-start.png`,
`no-browser-reboot.json`, and the corresponding check logs. The browser capture
documents test rendering, not an independent human observation or the versioned
essential frontend acceptance manifest. Transport-failure/backlog scenarios
were validated at isolated deterministic failure boundaries, not by damaging
the running instance's notification channels or journal.
