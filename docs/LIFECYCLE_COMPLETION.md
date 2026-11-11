# Lifecycle completion and timing ownership

Owner: PAO / Functions, with independent Remote and platform controller timing.
No protected Core source changes are part of this candidate.

A successful shared replacement receipt must match the current generation.
Local services must be ready, have no failed Agents or hard errors, and each
target Worker must pass the existing fresh PID/nonce/runtime/generation,
backend readiness, route acceptance and old-owner retirement checks.
Telegram, Remote and other external connector warnings do not replace this
proof and do not prevent success. The operation persists succeeded_with_warning
and translated follow-up text. Connector activation/retries run after local
commit and cannot hold the commit RPC open. Quiescence still cancels this task
before retiring its owner, preserving single ownership of ingress.

Core's existing preparation, activation, quiescence and process budgets remain
authoritative. PAO no longer adds a second 20-minute wall-clock verdict while
waiting for the Core result. A receipt is never inferred from elapsed time.
Recovery without handoff continuity, malformed receipts, generation mismatch
and hard local failures retain explicit unsuccessful results.

Qualified-cache admission checks the current checkout seal before scanning
the old immutable artifact. An ineligible checkout avoids a useless old-artifact
scan. A valid hit still verifies every artifact byte. Cold-start fallback retains
independent full artifact and runtime verification and never executes rejected
checkout bytes.

Remote restart receipts include bounded monotonic phase timings; notification
retries do not extend a completed lifecycle duration. The Windows restart task
records controller stop, task retirement/start, health polls and task queries.
Functions timing is documented in LIFECYCLE_TIMING.md. Parent and child intervals
overlap; use critical paths/unions and keep bootstrap intervals explicit.

NP measurements and evidence are candidate-specific. Local commit, actual loaded
version, test success, production deployment and user acceptance are separate facts.
Repeated runtime-file verification inside protected Core requires its own reviewed
Core-version migration; do not bypass admission checks from Functions.

Cold self-restart uses Remote's authenticated detached admission. HTTP 202 returns
one already-persisted restart ID; it is acceptance, never completion. Remote owns
the surviving executor, rejects overlapping launches, records terminal proof and
then attempts notification. Legacy synchronous and trusted-peer paths remain.
The existing owner-scoped operation API projects Remote's public receipt contract;
it cannot execute a restart or disclose another owner, Agent or instance's record.
Once terminal proof is persisted, follow-up notification/audit errors cannot
reverse it; they only add a warning. Receipt readers use bounded, short-lived
snapshots. Atomic Windows replacement retries brief sharing denials for at most
250 ms; persistent storage failures remain explicit and never publish partial data.
Malformed terminal evidence does not project success. Exact receipt reads survive
the recent-completion discovery window.

Windows restart opens one exact registered task before stopping the old process.
Its live Scheduler COM State/LastTaskResult reads replace repeated CIM discovery;
old-task retirement, health identity, crash detection and bounded polling remain.
Idle media context/route use authenticated GET routes; state-changing controls
retain POST. Shared quiescence separately counts live Calls and pending start
reservations across owners, including when no HTTP request is in flight. Failure
