# Core v5 private launch adapter checkpoint

Status: source checkpoint, independent human PR review pending; not a release.

This checkpoint follows the previously tested role/admission primitives. One
standard-library launch owner, `orchestrator.runtime_process`, now starts the
exact selected interpreter with an inherited private Connection and the
existing versioned JSON envelopes. Shared Functions, Agent Workers, qualification
probes, candidate probes and the existing recovery route use that owner.

The child reports a one-time nonce, observed PID, complete runtime fingerprint
and generation/request digest before any product import. The parent checks the
OS image and direct child relationship (including the Windows venv launcher),
then explicitly accepts admission. Only then can the child execute the
qualified generation under its immutable finder and candidate import guard.
Windows starts with `close_fds=True`, an explicit handle list and a short
inheritance lock. Owned Job objects retain descendant cleanup. POSIX uses a
private passed descriptor and a new process session. There is no global
`multiprocessing.set_executable`, pickle or localhost listener.

Native Windows evidence before this source publication:
- 13 real launch/fault cases passed: private message/event round trips, four
  simultaneous children, retained durable progress after a crash, fake
  executable/package/schema/PID/nonce/generation rejection before acceptance,
  and cancellation cleanup.
- 19 integrated launch/minimal-Core/kernel fixture cases passed in the preceding
  integration run; the full-product preparation case was explicitly deselected
  there and remains a required follow-up.
- Role/install/runtime and generation run: 112 passed, 2 failed. One failure
  correctly rejected uncommitted Function source; the other exposed the
  pre-existing compatibility allowance for unrelated dependency drift plus a
  missing error import. The launch fingerprint now observes the actual
  selected process while the existing Function comparator still checks the
  requested candidate contract. Both cases must be rerun after publication
  makes the exact source available to the normal commit-membership gate.
- Earlier red import failures and the fixture stop/response race are preserved
  in private evidence. The concurrent fixture now closes its real channel after
  the asserted echo; existing product stop/rollback assertions are unchanged.

This is not acceptance of all of Gate 2. Still required: distinct sealed CR/FR
environments, full product preparation and lifecycle qualification, native
handle/descendant fault qualification, supported Linux execution, complete
original-dependency functionality parity and release packaging.

Original standard dependency versions remain unchanged. PR #137 security
versions have not been integrated. No existing Core/service, active Python
environment, real task state or production checkout has been changed.

The user specifically chose to review source on GitHub instead of using a
review agent. No independent approval record or passing review claim is
fabricated. The existing Core release/merge check remains unsatisfied until
that review is recorded; draft source publication is not release approval.
