# Evidence Pack

## Goal

Leave a durable, secret-free record of the dual-channel session.

## Minimum fields (JSON)

```json
{
  "ok": true,
  "topic": "start-aptenra-smoke",
  "adjacency": "lan",
  "session_state": "dual",
  "channels_used": ["kvm", "remote"],
  "target_host": "192.168.0.41",
  "git_head": "optional short sha",
  "launcher_events": ["launch.ready"],
  "kvm_snapshots": ["path-or-hash"],
  "at": "ISO-8601"
}
```

## Lab location

`C:\AptenraDebug\evidence\<topic>-latest.json`

Optional mirror under operator debug-sync evidence tree for the device id.

## Rules

- No API keys, tokens, PINs, passwords.
- Prefer event names and counts over full log dumps.
- If logs must be attached, redact `.env` and credential lines first.
- One `*-latest.json` pointer plus optional timestamped archive.
