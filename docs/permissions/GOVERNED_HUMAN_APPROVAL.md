# Governed human approval from Workbench

Owner: PAO and shared Frontend Connector, in Functions. This document describes source behavior and required acceptance; it is not production activation or a substitute for the native Windows WAC protocol.

## Original request and independent human login

A configured installation may opt in to `global.permission_service.approval_origin`: an exact, operator-owned HTTPS origin for the HASHI Backend API; plain HTTP is accepted only on loopback. Other descriptor fields and pinned private service endpoints remain unchanged. No address is inferred from an incoming Host, relay HMAC, folder name or model text. Absence keeps the existing unavailable governed path. Production configuration is not changed by this PR.

HTTPS termination at an operator-owned reverse proxy is explicitly opt-in through
`global.permission_service.approval_proxy_peers`, a list of exact socket-peer IP
addresses (for a local proxy, typically `["127.0.0.1"]`). It requires the configured
HTTPS `approval_origin`; hostnames, CIDRs, unspecified and multicast peers are
rejected. The default empty list preserves direct-origin checks. No origin or
trust decision is inferred from `Host`, `Forwarded`, `X-Forwarded-For` or a relay
credential.

For a configured peer only, an HTTP upstream hop must preserve the configured
Host and exactly one `X-Forwarded-Proto: https` plus one matching
`X-Forwarded-Host`. The proxy must overwrite caller-supplied forwarded headers,
and its upstream should be locally bound or restricted to that trusted proxy.
This transport configuration does not authenticate a human or qualify approval
isolation. The public browser still uses HTTPS; Secure/HttpOnly/SameSite cookies,
exact form Origin, CSRF, invitation and independently authenticated owner checks
remain required. Proxy changes invalidate cached invitation presentations.

The existing shared /safety prompt produces an ordinary URL button when a governed session is missing. Stock Workbench already renders this generic button. The opaque invitation binds the original instance, owner, Agent, Session/generation, pending request/revision and FC connection. It authenticates nobody and grants nothing. Reopening a prompt after invitation expiry makes a new presentation, not a permission lease.

The URL opens the HASHI-owned approval page. Organisation, email and password are submitted by its same-origin form to the existing permission service login. Human sessions remain HttpOnly, SameSite=Strict, path-bound cookies (Secure on HTTPS). They never enter the URL, command schema, Worker envelope, page script, logs or model context. Same-origin validation, a separate double-submit CSRF nonce, escaped HTML, no-store, a same-origin referrer policy and restrictive CSP apply. The referrer policy preserves Origin on local form POSTs and suppresses cross-origin referrers; a null or foreign Origin is still rejected. No frontend-specific identity store, decision writer or permission policy is added.

## L1 is not governed L2

The service records only hashes of independently authenticated human sessions for its current generation: verified password login or the existing, explicitly configured original-native-human identity link. L2 issue/submit/manage calls require this provenance in addition to the existing live identity, exact owner, policy and PAO Run checks. A cooperative personal L1 session, a shared instance credential, an ordinary answer and an unrecognised surviving session cannot satisfy it. A policy switch does not promote an earlier L1 token. A service replacement requires fresh human authentication; no unaudited provenance is reconstructed from a bearer prefix. Logout and underlying identity revocation still apply.

The existing native-identity path retains its L1/L2 support only for an explicitly registered human link through the trusted original-command FC. Its original transport/user checks reject synthetic API actors before native login. Backend API/Workbench uses the separate governed password page; relay HMAC and the cooperative personal L1 login never acquire that provenance. A regression verifies native L2 approval, exact once scope and logout alongside synthetic-API and personal-L1 rejection.

## Exact decision, resume and retirement

After human login the page reads the original current Session/request and displays the requested resources/actions and policy-derived choices. Actions come from PermissionDecisionController; the page does not sign decisions or create grants. POST accepts only an action issued to that invitation and CSRF presentation. Gateway signatures, revision checks, the original operation digest and PAO Run fence remain authoritative.

An identical decision replay uses the existing durable action and result, not another grant or file operation. An outcome that may have committed but lost its response is shown as unconfirmed; that presentation cannot blindly submit again. The original operation resumes through its existing waiter/gate and verifies the lease again at use. Expiry, explicit revocation, account/session revocation and context changes retain their existing fences. The page's revoke action uses the original lease and authenticated management path. Decision, actual effect, physical resource cleanup and persisted frontend history are separate observations.

## Approval presentation contract

Frontend Connector Functions own the read-only structured request summary and
trusted HTML renderer. PAO remains the sole decision, lifetime and execution
owner. Pending discovery adds a version-1 `summary` containing the original purpose,
task/request state and exact resource kind, name, path and actions. It contains
no human identity, credential, decision action or inferred grant.

The trusted page shows the Agent, purpose, resource/action summary, a write
replacement warning, expandable full locations and internal request details.
Only policy-issued once approval and denial appear in the primary row; existing
task/Run/timed options remain behind More authorization options. Their opaque
IDs, scope and expiry are unchanged. The current Session summary supplies lease
status on refresh: consumed/expired/revoked leases have no stale revoke control.
A recorded decision never claims the operation completed or resources were
cleaned up. Resource cleanup pending remains a separate observation.

Theme/mode query hints affect appearance only. Theme names are allowlisted;
Simple uses its independent bright/dark/cli owner. Static presentation tokens
are a qualified snapshot generated by the private frontend's
`scripts/export_approval_theme.mjs`, not a runtime dependency on that repository.
The same-origin form and its login redirect retain only those safe hints.
The inline stylesheet is authorized by its exact SHA-256 in CSP; scripts,
external assets, framing and arbitrary style injection remain disallowed.
Expired invitations render a themed non-actionable status. Return guidance
points to the original conversation without accepting an arbitrary redirect.

The external frontend uses a compact pending card and an explicitly opened
Simple dialog, desktop conversation drawer or mobile bottom sheet. Deferral is
local presentation state, not denial; polling cannot reopen it. Generic legacy
command controls stay compatible, with exact opaque action submission. L2
credentials and decisions remain on the trusted page.

## Validation and remaining acceptance

`tests/test_permission_human_approval.py` covers actual HTTP password forms, a real TLS-to-HTTP proxy with certificate/hostname validation, explicit peer opt-in and forwarded-header rejection, HMAC/L1 rejection, owner/CSRF/origin boundaries, exact decision replay, expiry, revocation/logout and a committed-but-lost response. The service responsiveness tests hold a real ledger commit; transaction contention tests verify that an already waiting revocation commits before a later access poll. Existing native isolation tests remain mandatory.

A real stock Workbench browser qualification must include the URL button, password login, exact approval, the original operation's one effect, a replay without another effect, expiry/revocation against the same resource, and persisted final history after refresh. Record its exact HASHI source and Workbench artifact, identity source, use of automation, and cleanup. An automated fixture human is not a manual user acceptance receipt. A same-process file supervisor is not independent LPAC L2 isolation. Preserve all historical native failures/receipts at their original source SHA; final production activation remains on hold until formal acceptance passes.
