# Nonproduction repair review — 2026-11-14

Scope: whiteboard `workbench/diagnosis/recording-session-gateway-refused-20261114` and `hashi/network/hashi7-credential-mismatch-diagnostics`. Functions owners are the Exchange ingress service and Remote authentication projection. Protected Core and instance credentials are unchanged.

Exchange maintenance, scheduling and confirmation now run their synchronous inbox/session/outbox transactions on a serialized worker. Connection creation, lock wait, transaction completion and close stay on that worker. Recovery walks are serialized, cancellation drains an outstanding worker before releasing ownership, and an uncertain operation is not automatically replayed by this adapter. SQLite writer waits are bounded to 200 ms. The connection context explicitly closes each connection. Failure diagnostics contain the operation name and SQLite code/name, never payload, SQL text or credentials.

A real SQLite write lock reproduced an API event-loop pause of 406 ms in the previous source; the focused regression now keeps the heartbeat responsive while reporting contention. Other regressions cover explicit connection closure, cancellation while work is still running, durable acceptance, deduplication, confirmation and recovery. The original historical gateway incident still has insufficient evidence to name its unique lock holder.

Remote status/proxy rejection retains HTTP 401 and all HMAC verification/replay checks. Its public explanation identifies the target instance and authentication layer. It does not claim a shared credential has expired, nor infer a specific reason from an undifferentiated signature rejection. Correct credentials still succeed and replay still fails. Private HASHI7 remains private; no connection was enabled and no credential was copied to the shared network.

Workbench has a separate companion PR for startup cache, discovery recovery, safe authentication presentation, SSE cookie isolation, PWA caching and short-screen chat layout. Neither PR is a production adoption claim. The native bulk-text repair candidate and its validation boundary are described below.

## Local validation

95 focused ingress, real HTTP acceptance, recovery, cancellation, Remote gateway and HMAC checks passed, without skips. The test commands select `test_exchange_async_entry`, `test_exchange_recovery_responsiveness`, `test_exchange_ingress`, `test_exchange_remote_api`, `test_remote_workbench_gateway`, `test_remote_shared_token` and `test_workbench_auth_explanation`. Protected Core preflight passed.

Additional local-lab integration remains blocked: the installed independent Exchange source at 9ead88c7 does not negotiate `message_freshness_v1`, which the unchanged HASHI protocol already requires. After loading its declared websockets 16.0 into an isolated test-only directory, three lab flows fail with `UNSUPPORTED_CAPABILITY` before ingress acceptance. Upstream main a3a54963 also lacks that capability. No capability check was removed or forged to make this integration pass, and no running Python installation or Exchange checkout was changed. This additional limitation is not a product-fix pass.


## Native Windows bulk text follow-up

Whiteboard: `workbench/remote-desktop/windows-bulk-text-corruption`.
The unmodified native host reproduced the original modern Notepad failure:
`HASHI desktop recovery verified 2026-11-09` became
`HASHI oooooooooooovery 9999999999999999999` in a uniquely created temporary
document. The submitted text reached the adapter once and unchanged. The same
adapter preserved it in classic EDIT, system RichEdit and a standalone Office
RichEditD2DPT. Thus a generic RichEdit success does not certify Notepad's
application/TSF path. This locates the failure after native packet injection; it
does not establish a defect in every control or name an internal Windows cause.

The candidate avoids the failing packet path for the focused, writable Unicode
RichEditD2DPT only. Both the ordinary Windows adapter and the optional local host
use one undoable EM_REPLACESEL, with foreground thread/process and focus checks.
It does not read target text or touch the clipboard. Changed focus fails before
dispatch. A native message timeout is uncertain and never falls back to keyboard
input or repeats the edit. Other controls, read-only controls and secure-desktop
keyboard mapping preserve their existing behavior. Empty internal text remains
a no-op; the public protocol continues to reject empty or oversized text.

Four of six focused regressions failed before the candidate. The compiled real
host now inserts the original sample, Chinese/emoji, mixed newlines/tabs and the
4096-unit boundary into a hidden native control at its existing selection; one
undo restores that selection. The dispatch test also checks an uncertain timeout
and oversize rejection. Python routing tests check the target fence, read-only
and other-control fallback, UTF-16 supplementary characters and no retry after a
timeout. These component tests create no visible window and send no OS keyboard
or mouse input.

The user's subsequent constraint prohibits further direct desktop control. All
later page interaction runs in an independent headless Chromium process against
a local fixture that records operations and cannot reach a Computer Worker.
The candidate has not been re-driven in the full modern Notepad application
after that constraint. Hidden native-component correctness and browser forwarding
are separate evidence; neither is claimed as a full application acceptance.

Local follow-up validation: 85 focused native/desktop/permission-adjacent checks,
33 desktop client policy checks and 18 headless Chromium UI checks passed with
no skips or cancellations. The production-target native host also compiles as
an x64 Windows executable. No installed desktop host, production service,
credentials or protected Core were changed. No hosted workflow was requested.
