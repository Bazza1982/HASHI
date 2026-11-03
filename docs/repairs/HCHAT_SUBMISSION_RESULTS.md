# HChat submission results

The sender exposes immutable per-call submission facts. The original boolean
entry remains compatible: it means that the receiver handled the logical message,
including confirmation terminal states. PAO uses the detailed result and preserves
queued, waiting-for-confirmation, confirmed, discarded, expired and superseded
outcomes. It does not label every successful exchange as queued or executed.

A request-local ContextVar transports diagnostic facts through existing synchronous
route helpers without changing their signatures, selecting routes or granting
authority. Each invocation allocates a separate capture and resets it in finally;
nested group members and concurrent threads return separate immutable results.
Group receipts report accepted, unknown, terminal and rejected counts.

Known preflight failures retain their finite reason. A missing acknowledgement
remains unconfirmed and stops failover; an authoritative receiver rejection also
stops automatic routing to another connector. Proved connection refusals retain
the existing host fallback. Public receipts never copy raw exceptions, tokens or
network endpoints. A later bookkeeping failure cannot revoke a known receiver
result. Legacy injected senders returning False provide no qualified acceptance
evidence and therefore produce an unconfirmed result rather than a guessed cause.

Fixtures traverse the default sender through PAO receipt rendering and exercise
real route helpers, confirmation states, failed transport acknowledgements,
concurrent calls, partial groups and post-acceptance bookkeeping failures. They
never contact another Agent. Runtime adoption and production release are separate.
