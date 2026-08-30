# TUI Instance Switching

## Purpose and boundary

`/instance` lets one running TUI move between HASHI instances without depending
on the directory from which the command was launched. The repository containing
`tui.py` remains the launch instance and is always the default.

Supported targets are:

- the launch instance itself;
- another Windows, WSL, Linux, or macOS instance on the same machine; and
- a directly reachable LAN or private-overlay peer.

Every cross-instance target must be discovered by the launch instance's local
Hashi Remote, must have `handshake_accepted` state on both sides, and must
advertise `tui_proxy_v1`. There is no manual URL escape hatch and no public
internet Workbench routing.

## Transport and trust contract

```text
TUI
  -> launch Remote POST /tui/proxy (local-host callers only)
  -> peer Remote POST /protocol/tui (hashi-shared-hmac-v1)
  -> peer's local Workbench API
```

This shape is deliberate. A successful handshake proves Remote-to-Remote trust;
it does not make a LAN-bound Workbench API authenticated. The TUI therefore
never uses a peer's Workbench host or port directly.

The proxy accepts only these named operations:

| Operation | Local Workbench request |
| --- | --- |
| `health` | `GET /api/health` |
| `agents` | `GET /api/agents` |
| `chat` | `POST /api/chat` |
| `transcript_recent` | `GET /api/transcript/{agent}` |
| `transcript_poll` | `GET /api/transcript/{agent}/poll` |

Arbitrary paths are not represented in the protocol. Text, agent, offset,
limit, and response sizes are bounded before forwarding.

## Switch transaction

`/instance <id>` performs the following transaction:

1. Refresh the local Remote peer registry.
2. Require a live peer, `handshake_accepted`, and `tui_proxy_v1`.
3. Create a candidate API client without changing the active client.
4. Fetch candidate health and require the returned `instance_id` to match.
5. Fetch the candidate agent directory.
6. Commit the new client, increment the connection generation, clear the old
   agent/broadcast/transcript state, and select the first active peer agent.

Any failure before step 6 leaves the current connection unchanged. Polling,
initial transcript loads, onboarding wakeups, and sends carry the connection
generation or client reference so stale results cannot appear in the new
instance view.

Transcript byte offsets live inside each API client and are never copied during
a switch. The chat panel is cleared at commit. The log panel is not remote: it
continues to follow the launch repository and is labeled `Local log — <id>`.

## Commands

```text
/instance             list current and discovered instances
/instance <id>        switch to a trusted peer
/instance current     return to the launch instance
/instance refresh     refresh and list peers
```

Remote online and TUI available are distinct states. A peer may be visible but
unavailable because the handshake is incomplete, it is offline, or its Remote
has not yet been upgraded/restarted to advertise `tui_proxy_v1`.

## Rollout compatibility

Existing `python tui.py`, `/to`, `/agents`, `/log`, and `/quit` behavior remains
available. A Remote process must be restarted after deploying this version so
its advertisement and routes include `tui_proxy_v1`; the HASHI core process does
not need to be restarted for this capability update.
