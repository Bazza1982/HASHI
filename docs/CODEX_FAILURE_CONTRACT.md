# Codex CLI failure contract

HASHI treats the JSONL stream from `codex exec --json` as the authoritative
protocol.  stderr is only a fallback when Codex exits without a structured
terminal event.  This follows the official OpenAI documentation, which lists
`turn.completed`, `turn.failed`, and `error` as distinct non-interactive event
types: <https://learn.chatgpt.com/docs/non-interactive-mode>.

## Adapter behavior

`turn.failed.error` is decoded even when the provider nests another JSON error
object inside the message string.  A failed `BackendResponse` carries:

- the exact provider message in `error`;
- a stable `error_code` and `error_retryable` decision;
- `http_status`, `provider_request_id`, and `retry_after_s` when supplied or
  unambiguously recoverable;
- unique `tool_call_count` and conservative `side_effects_possible` evidence;
- a short provider-neutral description in `stream_metadata`.

Top-level `error` events are retained as diagnostics but are not terminal by
themselves: Codex also uses them while reconnecting.  A later
`turn.completed` therefore remains successful.  A `turn.failed` event is
terminal and starts the same bounded subprocess-exit grace period as
`turn.completed`.

Current stable classifications include:

| Code | Typical condition | Retryable |
|---|---|---:|
| `CONTEXT_CAPACITY_REJECTED` | Serialized request exceeds context capacity | No direct replay |
| `PROVIDER_CAPACITY_UNAVAILABLE` | Selected model is temporarily at capacity | Yes |
| `PROVIDER_QUOTA_EXHAUSTED` | Account usage allowance is exhausted | Only with an unambiguous numeric delay |
| `PROVIDER_RATE_LIMITED` | Provider rate limit | Yes |
| `PROVIDER_INCOMPLETE_STREAM` | Connection/stream ended before completion | Yes |
| `PROVIDER_REQUEST_TIMEOUT` | Provider or idle timeout | Yes |
| `PROVIDER_SERVER_ERROR` | HTTP 5xx provider failure | Yes |
| `PROVIDER_AUTHENTICATION_FAILED` | Invalid or missing credentials | No |
| `PROVIDER_PERMISSION_DENIED` | Account/model access denied | No |
| `PROVIDER_MODEL_UNAVAILABLE` | Invalid or unsupported model | No |
| `PROVIDER_SAFETY_REJECTED` | Provider safety rejection | No |
| `PROVIDER_BAD_REQUEST` | Other invalid request | No |
| `PROVIDER_EMPTY_RESPONSE` | Successful exit without deliverable content | Yes |
| `PROVIDER_UNKNOWN` | No stable classification was possible | No |

`error_retryable=true` never proves that replay is safe.  Runtime retry logic
must also require `side_effects_possible=false` and no observed tool activity.
Collaboration tool calls are treated as potentially state-changing.  A Codex
diagnostic reporting dropped provider events also fails closed because the
missing interval could contain an unobserved command or file mutation.

## Sessions and concurrency

Transient failures preserve a newly reported Codex thread ID so a later retry
can resume it.  `CONTEXT_CAPACITY_REJECTED` clears that provider thread before
HASHI's one typed compaction retry; resuming the rejected full thread would
recreate the same overflow.

The API Gateway pools adapters by `(engine, model)`.  A pooled adapter's model
is immutable, and each entry has an exclusive request lease.  Calls using the
same engine and model serialize; different model entries can run independently.
The Codex adapter also has its own lock for callers outside the Gateway.
Requests sharing a server-side `session_id` also serialize before reading the
session cache, so a concurrent second turn cannot miss the first turn's reply.

## API propagation

Synchronous `/v1/chat/completions` failures use the backend's typed HTTP status
and an OpenAI-compatible error object:

```json
{
  "error": {
    "message": "Selected model is at capacity.",
    "type": "server_error",
    "code": "PROVIDER_CAPACITY_UNAVAILABLE",
    "status": 503,
    "metadata": {
      "retryable": true,
      "provider_request_id": "req_example"
    }
  }
}
```

This replaces the legacy text-only `{"error": "..."}` response.  Consumers
must read `error.message`; consumers that already handle the Gateway's
structured/media errors need no separate path.

Streaming HTTP headers are already committed before a late provider failure,
so the transport status remains 200.  HASHI emits an SSE `error` object followed
by `[DONE]`; it does not emit a successful `finish_reason: stop`, persist the
partial answer in the session cache, or treat partial text as a completed turn.

Foreground, background, and scheduled runtime listeners receive the same typed
fields.  Telegram delivery intentionally retains the provider's readable error
message rather than exposing a JSON envelope.

## Diagnostic retention

`workspaces/<agent>/codex_exec_events.jsonl` is now written incrementally,
redacted, and rotated.  Defaults are 8 MiB per file plus two backups; one event
is capped at 64 KiB and replaced by a hash/preview envelope when larger.
Credential-shaped values and user-home prefixes are redacted before writing.
Commands, tool inputs/outputs, prompts, reasoning, and agent-message text are
stored only as byte counts and SHA-256 receipts; file-change paths and provider
failure metadata remain available for diagnosis.
Legacy current/backup logs are sanitized once on the first new Codex request
after the code is loaded; oversized files retain only a bounded sanitized tail.

Per-agent overrides live in backend `extra` configuration:

- `codex_event_log_max_bytes`
- `codex_event_log_backups`
- `codex_event_log_max_event_bytes`

Changing these values does not alter provider output or retry policy.
