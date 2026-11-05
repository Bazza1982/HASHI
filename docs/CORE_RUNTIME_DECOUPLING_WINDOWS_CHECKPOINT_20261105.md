# Core v5 atomic Windows launch checkpoint

Status: review-only source. This supplements the role/split checkpoints, without
claiming complete Gate 2/3, security-dependency adoption or release readiness.

Windows source CI found a real race: a venv redirector could create its base
interpreter before the parent assigned the redirector's Job. Admission correctly
rejected that unowned interpreter. A deliberately delayed ownership test
reproduced the same OS failure locally; no unexpected receipt was accepted.

The launch owner now creates and configures its Job first, then calls the
documented CreateProcessW extended-startup interface with JOB_LIST and an
explicit inherited-handle whitelist in the same operation. The interpreter and
its redirector cannot execute before assignment. This also avoids the suspended
orphan window of a separate create-suspend-assign-resume sequence. No debugger,
extra privilege, global spawn-executable override or localhost listener is used.

Only the private IPC channel and explicitly duplicated standard I/O are
inherited, matching Popen's redirected-stdin behavior. The Job handle itself is
not inherited. Temporary standard handles, process/thread handles and attribute
lists have explicit retirement. Existing ancestor Job restrictions remain in
force; native tests observe that membership before crashing the controller.
Admission still checks the real OS image and parent, exact role fingerprint,
one-time nonce and immutable generation before product execution. A child
outside its already assigned Job is rejected.

The process wrapper observes asynchronous Job termination before trying another
termination. Cancellation, lifecycle and recovery continue to use the same
process interface and JSON envelopes. POSIX creation remains unchanged and
still needs its separate native qualification.

Native fault evidence includes:
- the original CI and deterministic local pre-ownership race failure;
- real simultaneous starts, round trips, unsolicited events, crash persistence,
  rejected executable/package/schema/PID/nonce/generation and cancellation;
- abrupt controller death immediately after process creation and after a worker
  starts a real long-lived grandchild, with every observed descendant retired;
- preservation of the creator's existing Job restrictions;
- strict OS HANDLE counts across repeated completed launches, without a tolerance.

Disabling only the test controller's child Job kill limit makes the actual
parent-crash assertion fail; the test's independent outer owner then retires all
recorded children. Disabling process-handle retirement makes the OS count
increase and fails the handle test. These private mutations never change
production or installed environments. Their failures and cleanup receipts are
preserved.

CI also exposed ambient project build metadata during zero-dependency Core
observation. The native fixture now copies the exact protected source artifact
and role policies, asserts its Core digest equals the candidate, and qualifies
that clean artifact. It does not ignore installed packages or change the
zero-package assertion. A separate real-interpreter proof accepts the clean
artifact and rejects the identical Core plus a source build egg-info directory.
Installer observation now includes the actual typed rejection diagnostic.
Native CI retains JUnit plus observed CR/FR/FG identities and handoff receipts.

The earlier original-dependency sealed-role run passed three cases, including a
same-Core test-package update/rollback. After the ownership changes its final
native and complete business qualification still need revalidation. The old
82cb292f candidate's complete offline run was explicitly stopped after CI proved
the ownership defect; its partial cases and prior batch time limit are retained,
not reported as a full-suite pass.

Original application dependency versions are unchanged. PR #137, full business
Gate 3, packaging/installation authority protection, independent same-tree human
Core review, Linux qualification and operator adoption remain HOLD. No active
environment, existing Core/service, real user task or production source has been
changed. Draft publication on the authorized branch is for the requested human
review and does not waive the existing merge/release guard.

The final atomic launch byte tree has since passed 124 focused role, seal, IPC,
cancellation, installation and Core-boundary cases, plus three real Windows
sealed-role cases using the unchanged original application dependencies. The
observed Core PID stayed constant through the fixture dependency update, failed
candidate and explicit rollback; durable effects remained exactly once. This
fixture is not the PR #137 security package upgrade.

The complete product cold qualification and prepare also passed in the separate
zero-dependency Core and original-dependency Functions runtimes: 666 modules and
728 resources, 77.735 seconds cold qualification and 26.062 seconds prepare.
No external product route was activated. The complete offline 9,032-case scope
reached its 1,800-second outer deadline after about 66%; the preserved exit 124
and incomplete JUnit do not constitute a suite pass. A full-scope rerun has a
5,400-second outer budget, retaining the same markers and product timeouts.

Windows CI now runs that complete offline scope after native role qualification
and retains its JUnit even on failure. HCC CI recognizes the explicit Core-major
authorization label and passes both existing guard flags; it still invokes the
same major-version and independent exact-tree review checks. Real execution of
its three authorization states rejects missing/unrecognized authorization and
continues to reject the authorized candidate for its missing independent review.
It does not mark source review, Gate 3 or release accepted.
