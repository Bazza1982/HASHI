# TUI Instance Switching

## Current-instance scope

The selected instance is the highest TUI routing scope. A switch validates
the candidate identity and its complete Agent-directory response before one
connection generation is committed. Failed or stale checks leave the prior
client, Agent, transcript cursor, and send target unchanged. `/agents`,
`/to <agent>`, and `/to all` consume only that generation's directory;
directory errors are never represented as an empty directory and an empty
broadcast target set is an error. A submission freezes its client,
generation, instance-owned Agent set, mirror choice, and locale before I/O.

The local preference file remembers only the last successful single-Agent
selection for each instance. Startup still begins on the launch instance;
manual instance switches restore that instance's remembered Agent when it is
still present. `ALL` is never persisted.

The host log follows the current instance by default. Authenticated peer logs
use the restricted `log_tail` Remote operation, which exposes a bounded line
tail and opaque byte cursor but no filesystem path. Switching generations
cancels the old follower and clears its rendered buffer. `/log local` is the
explicit exception for inspecting the launch computer; `/log current`
returns to current-instance following. An unavailable remote log is shown as
unavailable and never silently replaced by the launch-instance log.

Persistent Session status is separately discovered from
`/api/v1/capabilities` for each connection generation. Chat remains valid
when that optional capability is disabled; the TUI does not issue Run-status
requests or render a task failure in that case.

Architecture: [HASHI Frontend Connector Architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md)

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
internet Backend API routing.

## Transport and trust contract

```text
TUI
  -> launch Remote POST /tui/proxy (local-host callers only)
  -> peer Remote POST /protocol/tui (hashi-shared-hmac-v1)
  -> peer's local Backend API
```

This shape is deliberate. A successful handshake proves Remote-to-Remote trust;
it does not make a LAN-bound Backend API authenticated. The TUI therefore never
uses a peer's Backend API host or port directly.

The proxy accepts only these named operations:

| Operation | Local Backend API request |
| --- | --- |
| `health` | `GET /api/health` |
| `capabilities` | `GET /api/v1/capabilities` |
| `agents` | `GET /api/agents` |
| `agent_overview` | `GET /api/agents/{agent}/overview` |
| `scheduler_jobs` | `GET /api/agents/{agent}/scheduler/jobs` |
| `background_jobs` | `GET /api/background-jobs?agent={agent}` |
| `chat` | `POST /api/chat` |
| `chat_attachment` | one bounded `POST /api/chat` carrying frozen bytes or a target Workzone reference |
| `voice_state` / `voice_profile` | `POST /api/tui/voice` against the selected Agent's existing voice owner |
| `speech` | `POST /api/tui/speech`; returns bounded authenticated Ogg bytes, never a remote path |
| `transcript_recent` | `GET /api/transcript/{agent}` |
| `transcript_poll` | `GET /api/transcript/{agent}/poll` |
| `log_tail` | bounded instance-owned `logs/bridge.log` tail; no path returned |

The three information-panel operations are read-only and Agent-scoped.
Arbitrary paths are not represented in the protocol. Text, agent, offset,
limit, attachment bytes, and response sizes are bounded before forwarding.
An attachment selected on the TUI computer is snapshotted before submission,
sent as authenticated bytes with size and SHA-256 integrity, and admitted to
the same target Agent media Run as its caption. A Workzone `@file` reference is
resolved only by the target instance beneath that Agent's active Workzone;
the source machine's path is never forwarded. Switching instance or Agent
invalidates a pending attachment and late generations are discarded.

`/say` and TUI auto-read are local presentation operations, not Agent chat
commands. Speech is generated on the selected instance using the Agent's
shared semantic voice profile, integrity-checked at the TUI, then played only
on the launch computer. It does not enter the Conversation or any Telegram
delivery path. One generation-fenced player task owns playback; a newer
request, target switch, auto-read Off, or exit cancels it before another asset
may play. Auto-read is a launch-client preference namespaced by instance and
Agent; the semantic voice profile remains Agent-owned shared state.

The TUI is HASHI's permanent built-in reference terminal Connector. This
switching contract currently proxies the basic Backend API chat/transcript
surface; it does not claim complete Persistent Session API v1 support.

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
/attach <path>        snapshot one local file for the next message
/attach clipboard     snapshot a local clipboard image for the next message
/attach cancel        discard the pending attachment
@relative/path        attach one file from the target Agent's active Workzone
```

Remote online and TUI available are distinct states. A peer may be visible but
unavailable because the handshake is incomplete, it is offline, or its Remote
has not yet been upgraded/restarted to advertise `tui_proxy_v1`.

## Rollout compatibility

Existing `python tui.py`, `/to`, `/agents`, `/log`, and `/quit` behavior remains
available. A Remote process must be restarted after deploying this version so
its advertisement and routes include `tui_proxy_v1`; the HASHI core process does
not need to be restarted for this capability update.
