# Nightly repair: Provider evidence and bounded HER recovery

Date: 2026-09-12

Branch: `repair/nightly-batch-20260911`

Checklist owners: HN-20260909-002/003, HN-20260910-001,
HN-20260911-004/005

## Implemented contract

- An OpenAI-compatible request is durably observed before network I/O. Its
  response, failure or cancellation is then recorded at the physical-call
  boundary. Authentication refresh records both the rejected response and the
  replacement request.
- Streaming HTTP errors are read while the response is open. Evidence keeps
  `empty`, `not_read`, `partial` and `read_failed` distinct, including declared
  and observed byte counts. Raw SSE events and tool-call assembly remain in the
  restricted evidence store; ordinary projections expose only safe references.
- Credentials and cookies are removed at capture time. A canonical runtime
  audit store is preferred; direct adapter diagnostics use a private append-only
  Agent-local file. Failure to persist the request evidence prevents network or
  tool side effects.
- OpenRouter, DeepSeek, xAI Chat/Responses and Ollama share the same protocol
  owner. Embedded SSE errors and invalid JSON remain typed failures rather than
  being flattened into a missing finish reason or fabricated success.
- DeepSeek malformed-tool recovery preserves the Provider's exact
  `reasoning_content`. Format repair and transient transport recovery share one
  incident budget of at most three additional physical requests. Switching
  error category cannot reset the budget, and completed tools are never replayed.
- Unique restricted evidence has no time-based deletion. Rotation applies only
  to safe operational projections, never to the sole complete original.

## Focused verification

The repair uses real `httpx.AsyncByteStream` fixtures for an initially unread
HTTP 400 body, fragmented Unicode content, incomplete streams, cancellation,
SSE errors and tool-call fragments. Tests also assert credential exclusion,
fail-closed evidence persistence, DeepSeek thinking-carrier preservation,
shared recovery positions and control-flow propagation across tool-capable
adapter families.

Native Windows/WSL permissions and the authorised, low-volume real Provider
canaries are recorded in `NIGHTLY_20260912_RUNTIME_ACCEPTANCE.md`; unit evidence
alone does not close those rows.

## Protected boundary

No protected Core file is modified. Run
`python scripts/check_protected_core_changes.py --base
19e330f985aaf6d5523fd82a3bcc8a4256b536c0` before accepting this batch.
