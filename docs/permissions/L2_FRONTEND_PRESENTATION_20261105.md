# L2 permission presentation and current acceptance scope (2026-11-05)

Owner: Frontend Connector; engineering layer: replaceable Functions.
PAO owns the permission ledger and decides whether an action is available.
This correction changes only the safety command's error projection.

## Observed defect

On Intel, the unchanged external Workbench at
`aaedbd50d726577263b74ef662a32e7f7131bf20` displayed a real pending L2
request but offered no approval control. Its command response reported
`safety_presentation_io_error`. Zero decisions, execution claims and file
effects do not, by themselves, prove why a request was blocked.

The private permission RPC projects service failures to typed codes, then the
client raises `PermissionRemoteError`, an `OSError` subclass. Previously the
renderer classified that wrapper as local storage I/O. A signed, real-code
RPC round trip reproduces the mismatch: an empty human session reaches
`SessionDecisionDenied`, returns that exact service code, and is rendered as
`io_error`. The valid-session control issues actions without granting access.

## Narrow correction and validation

Only a **read-only presentation**, before any mutation, maps the closed
permission refusal codes to `scope_unavailable`, revision/operation conflicts
to `state_changed`, and other remote errors to `service_unavailable`.
Arbitrary remote strings, tokens and paths are never shown. Actual local
storage errors retain their I/O category.

No authority, authentication, lease, claim, replay or recovery behavior changes.
An error after a possible mutation remains **unconfirmed**, including a typed
remote refusal; the renderer cannot infer that an earlier write did not occur.
The existing rejection/uncertainty tests remain in force.

Focused regression:
`tests/test_permission_remote_presentation.py`,
`tests/test_safety_failure_details.py`,
`tests/test_permission_api_decisions.py`,
`tests/test_permission_private_rpc.py`,
`tests/test_permission_safety_command.py`.
The pre-fix projection produced five genuine assertion failures; the initial
nine-case addition plus direct consumers passed 40 checks after correction.
An additional native L1-refusal category is retained in the final matrix.
Fixture setup failures are preserved separately, not counted as product RED.

## Acceptance evidence and release boundary

The preceding complete HASHI version
`19b25ee2b40aa548fb7b63773e1210a9ce622542` passed 2,332 Windows integrated
checks (15 platform skips), 205 Linux checks (16 Windows-only skips), curated
Core and both current GitHub permission jobs. Its Intel whole-host matrix
passed 61 checks: schema 8/9 service upgrade, pre/post-commit process crashes,
legacy refusal, compatible recovery, L0 while Safety is stopped, real clock
changes and S3, stale-backup refusal and exact recovery replay. Three concurrent
LPAC tasks passed 35 independent checks; same/different/other-operation native
once races passed; unmodified Workbench L1 approval resumed the original Run
and produced exactly one effect. These are version-scoped observations, not
automatic qualification of every WAC case or a later commit.

The unchanged Workbench command proxy does not carry a governed human session
through its command path. Personal L1 remains usable through its existing
cooperative trusted-frontend mechanism; that mechanism must continue to refuse
L2. A positive governed-human L2 approval through this named Workbench is
therefore a remaining frontend acceptance gate. Do not fabricate that session,
inject a bypass, or treat an instance HMAC as human approval.

Current complete-HEAD receipts, native source/capsule identity, final resource
reclamation, exact failures and residual WAC cases belong in the final PR #133
review. Keep the PR Draft and L2 release HOLD until the remaining formal
platform/frontend combinations are actually qualified.
