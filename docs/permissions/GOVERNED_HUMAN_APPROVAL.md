# Governed human approval from Workbench

Owner: PAO and shared Frontend Connector, in Functions. This document describes source behavior and required acceptance; it is not production activation or a substitute for the native Windows WAC protocol.

## Original request and independent human login

A configured installation may opt in to `global.permission_service.approval_origin`: an exact, operator-owned HTTPS origin for the HASHI Backend API; plain HTTP is accepted only on loopback. Other descriptor fields and pinned private service endpoints remain unchanged. No address is inferred from an incoming Host, relay HMAC, folder name or model text. Absence keeps the existing unavailable governed path. Production configuration is not changed by this PR.

The existing shared /safety prompt produces an ordinary URL button when a governed session is missing. Stock Workbench already renders this generic button. The opaque invitation binds the original instance, owner, Agent, Session/generation, pending request/revision and FC connection. It authenticates nobody and grants nothing. Reopening a prompt after invitation expiry makes a new presentation, not a permission lease.

The URL opens the HASHI-owned approval page. Organisation, email and password are submitted by its same-origin form to the existing permission service login. Human sessions remain HttpOnly, SameSite=Strict, path-bound cookies (Secure on HTTPS). They never enter the URL, command schema, Worker envelope, page script, logs or model context. Same-origin validation, a separate double-submit CSRF nonce, escaped HTML, no-store, a same-origin referrer policy and restrictive CSP apply. The referrer policy preserves Origin on local form POSTs and suppresses cross-origin referrers; a null or foreign Origin is still rejected. No Workbench-specific identity store, permission API schema or client business logic is added.

## L1 is not governed L2

The service records only hashes of independently authenticated human sessions for its current generation: verified password login or the existing, explicitly configured original-native-human identity link. L2 issue/submit/manage calls require this provenance in addition to the existing live identity, exact owner, policy and PAO Run checks. A cooperative personal L1 session, a shared instance credential, an ordinary answer and an unrecognised surviving session cannot satisfy it. A policy switch does not promote an earlier L1 token. A service replacement requires fresh human authentication; no unaudited provenance is reconstructed from a bearer prefix. Logout and underlying identity revocation still apply.

The existing native-identity path retains its L1/L2 support only for an explicitly registered human link through the trusted original-command FC. Its original transport/user checks reject synthetic API actors before native login. Backend API/Workbench uses the separate governed password page; relay HMAC and the cooperative personal L1 login never acquire that provenance. A regression verifies native L2 approval, exact once scope and logout alongside synthetic-API and personal-L1 rejection.

## Exact decision, resume and retirement

After human login the page reads the original current Session/request and displays the requested resources/actions and policy-derived choices. Actions come from PermissionDecisionController; the page does not sign decisions or create grants. POST accepts only an action issued to that invitation and CSRF presentation. Gateway signatures, revision checks, the original operation digest and PAO Run fence remain authoritative.

An identical decision replay uses the existing durable action and result, not another grant or file operation. An outcome that may have committed but lost its response is shown as unconfirmed; that presentation cannot blindly submit again. The original operation resumes through its existing waiter/gate and verifies the lease again at use. Expiry, explicit revocation, account/session revocation and context changes retain their existing fences. The page's revoke action uses the original lease and authenticated management path. Decision, actual effect, physical resource cleanup and persisted frontend history are separate observations.

## Validation and remaining acceptance

`tests/test_permission_human_approval.py` covers actual HTTP password forms, HMAC/L1 rejection, owner/CSRF/origin boundaries, exact decision replay, expiry, revocation/logout and a committed-but-lost response. The service responsiveness tests hold a real ledger commit; transaction contention tests verify that an already waiting revocation commits before a later access poll. Existing native isolation tests remain mandatory.

A real stock Workbench browser qualification must include the URL button, password login, exact approval, the original operation's one effect, a replay without another effect, expiry/revocation against the same resource, and persisted final history after refresh. Record its exact HASHI source and Workbench artifact, identity source, use of automation, and cleanup. An automated fixture human is not a manual user acceptance receipt. A same-process file supervisor is not independent LPAC L2 isolation. Preserve all historical native failures/receipts at their original source SHA; final production activation remains on hold until formal acceptance passes.
