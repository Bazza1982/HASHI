# Nonproduction repair review — 2026-11-14

Scope: whiteboard `workbench/diagnosis/recording-session-gateway-refused-20261114` and `hashi/network/hashi7-credential-mismatch-diagnostics`. Functions owners are the Exchange ingress service and Remote authentication projection. Protected Core and instance credentials are unchanged.

Exchange maintenance, scheduling and confirmation now run their synchronous inbox/session/outbox transactions on a serialized worker. Connection creation, lock wait, transaction completion and close stay on that worker. Recovery walks are serialized, cancellation drains an outstanding worker before releasing ownership, and an uncertain operation is not automatically replayed by this adapter. SQLite writer waits are bounded to 200 ms. The connection context explicitly closes each connection. Failure diagnostics contain the operation name and SQLite code/name, never payload, SQL text or credentials.

A real SQLite write lock reproduced an API event-loop pause of 406 ms in the previous source; the focused regression now keeps the heartbeat responsive while reporting contention. Other regressions cover explicit connection closure, cancellation while work is still running, durable acceptance, deduplication, confirmation and recovery. The original historical gateway incident still has insufficient evidence to name its unique lock holder.

Remote status/proxy rejection retains HTTP 401 and all HMAC verification/replay checks. Its public explanation identifies the target instance and authentication layer. It does not claim a shared credential has expired, nor infer a specific reason from an undifferentiated signature rejection. Correct credentials still succeed and replay still fails. Private HASHI7 remains private; no connection was enabled and no credential was copied to the shared network.

Workbench has a separate companion PR for startup cache, discovery recovery, safe authentication presentation, SSE cookie isolation, PWA caching and short-screen chat layout. Neither PR is a production adoption claim. Windows native bulk-text corruption is not repaired by this change; the original task retains its diagnostic boundary.

## Local validation

95 focused ingress, real HTTP acceptance, recovery, cancellation, Remote gateway and HMAC checks passed, without skips. The test commands select `test_exchange_async_entry`, `test_exchange_recovery_responsiveness`, `test_exchange_ingress`, `test_exchange_remote_api`, `test_remote_workbench_gateway`, `test_remote_shared_token` and `test_workbench_auth_explanation`. Protected Core preflight passed.

Additional local-lab integration remains blocked: the installed independent Exchange source at 9ead88c7 does not negotiate `message_freshness_v1`, which the unchanged HASHI protocol already requires. After loading its declared websockets 16.0 into an isolated test-only directory, three lab flows fail with `UNSUPPORTED_CAPABILITY` before ingress acceptance. Upstream main a3a54963 also lacks that capability. No capability check was removed or forged to make this integration pass, and no running Python installation or Exchange checkout was changed. This additional limitation is not a product-fix pass.
