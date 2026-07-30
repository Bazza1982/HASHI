# Workbench Notes

## Shared Runtime Semantics

The Telegram bots and the local workbench are two frontends for the same in-process agent runtimes.

That means:

- both interfaces enqueue into the same per-agent runtime queue
- both interfaces share the same backend session state
- transcript order reflects queue order, regardless of whether a request came from Telegram, the workbench API, or the scheduler
- control commands affect the same shared backend session

Examples:

- `/new` resets the shared backend session for that agent
- `/model` changes the active model used by both Telegram and workbench traffic
- flex backend switches affect the same shared flex runtime
- `/resend` replays the last model or Bridge output without changing runtime state
- `/retry` intentionally replaces stale shared context with a clean `/new` or
  `/fresh` context, restores bounded handoff continuity, then reruns the last
  prompt; see [RETRY_RESEND_COMMANDS.md](RETRY_RESEND_COMMANDS.md)

## Operational Notes

- fixed agents write transcripts to `conversation_log.jsonl`
- flex agents write transcripts to `transcript.jsonl`
- the Python workbench API is optional infrastructure; if it fails to bind, the Telegram bridge should still start normally
