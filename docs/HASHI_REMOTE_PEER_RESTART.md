# HASHI Remote Peer Cold Restart

## Contract

Supported HASHI instances can cold-restart themselves or a trusted peer through
Hashi Remote without depending exclusively on WatchTower.

Commands:

```text
/restart
/restart HASHI2
/restart @hashi2
```

`/restart` targets the local HASHI instance. `/restart INSTANCE` targets another
HASHI instance.

## Safety gates

Peer cold restart is available only when all of these are true:

1. the source Remote peer registry records the target as `handshake_accepted`;
2. the accepted handshake advertises `rescue_restart`;
3. a live capability probe still reports `rescue_restart`;
4. the target Remote reports `remote_supervisor.mode=supervised`;
5. an authenticated live `/health` view from the target still records the
   source as `handshake_accepted`;
6. the restart request is authenticated by the existing Remote shared-token
   HMAC path; and
7. the target Remote has `L3_RESTART` enabled, which is also what causes it to
   advertise `rescue_restart`.

The provider is revalidated immediately before the destructive POST. Stale
source-side peer state or a stale capability cache is therefore not sufficient
authority for a cold restart.

A target Remote still owns the actual process lifecycle. The requesting HASHI
never receives arbitrary process-control or shell authority on the target.

## Local restart provider order

For a local `/restart`:

1. use the local supervised Hashi Remote when it advertises `rescue_restart`;
2. otherwise retain the existing WatchTower hard-restart path as fallback.

This means WatchTower remains useful but is no longer the mandatory restart
controller when a rescue-grade local Remote is present.

## Peer restart flow

```text
HASHI-A Core
    |
    | /restart HASHI-B
    v
HASHI-A trusted peer state
    |
    | accepted handshake + rescue_restart
    v
HASHI-B authenticated /health
    |
    | HASHI-A is still handshake_accepted
    v
HASHI-B Remote (OS supervised)
    |
    | fixed /control/hashi/restart
    v
HASHI-B Core stop -> start -> health verification
```

The source rechecks its accepted handshake projection, the target's live
capabilities, and the target's current trusted peer view immediately before
issuing the restart request.

## Supported Remote configuration

A rescue-grade target Remote must be OS supervised and allow L3 restart:

```yaml
security:
  max_terminal_level: "L3_RESTART"
```

Windows supervised Remote:

```powershell
.\bin\hashi_remote_ctl.ps1 install
.\bin\hashi_remote_ctl.ps1 start
.\bin\hashi_remote_ctl.ps1 status
```

Linux/WSL supervised Remote:

```bash
bin/hashi-remote-ctl.sh install
bin/hashi-remote-ctl.sh start
bin/hashi-remote-ctl.sh status
```

A child Remote started only by the HASHI Core is intentionally rejected as a
cold-restart provider because it may disappear with the Core it is meant to
rescue.

## Local acceptance test

Use two supported instances, for example `HASHI1` and `HASHI2`.

1. Start both HASHI cores.
2. Start both Remotes in supervised mode with `L3_RESTART`.
3. Complete the normal Hashi Remote handshake and confirm both peers show the
   accepted state.
4. From HASHI1 run:

```text
/restart HASHI2
```

5. Confirm HASHI2 Remote remains reachable while HASHI2 Core is stopped and
   started again.
6. Confirm HASHI2 Backend API returns healthy after restart.
7. Inspect HASHI2 `logs/remote_rescue_audit.jsonl` and restart logs.
8. Repeat in the reverse direction.
9. Negative-test by removing trust or lowering the target to `L2_WRITE`; the
   peer `/restart` request must be rejected before a destructive POST.

## Compatibility

Older Remote versions that do not advertise `rescue_restart` are treated as
unsupported, not as broken peers. The existing WatchTower path remains the
fallback for local restart where configured.
