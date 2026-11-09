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
4. the target Remote is currently running and owns the exact target instance;
5. an authenticated live `/health` view from the target still records the
   source as `handshake_accepted`;
6. the restart request is authenticated by the existing Remote shared-token
   HMAC path; and
7. the target Remote has its exact-instance fixed restart launcher installed.
   The normal terminal policy does not gate this authenticated operation.

The provider is revalidated immediately before the destructive POST. Stale
source-side peer state or a stale capability cache is therefore not sufficient
authority for a cold restart.

A target Remote still owns the actual process lifecycle. The requesting HASHI
never receives arbitrary process-control or shell authority on the target.

## Local restart provider order

For a local `/restart`:

1. use the running local Hashi Remote when it advertises `rescue_restart`;
2. otherwise retain the existing WatchTower hard-restart path as fallback.

This means WatchTower remains useful but is no longer the mandatory restart
controller when a trusted, restart-capable local Remote is present. Child and
supervised launch modes are both valid; mode is diagnostic, not authority.

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
HASHI-B Remote (running and authenticated)
    |
    | fixed /control/hashi/restart
    v
HASHI-B Core stop -> start -> health verification
```

The source rechecks its accepted handshake projection, the target's live
capabilities, and the target's current trusted peer view immediately before
issuing the restart request.

## Supported Remote configuration

A restart provider must be running, authenticated, and have its fixed lifecycle
installation ready. The default terminal policy is sufficient:

```yaml
security:
  max_terminal_level: "L2_WRITE"
```

Windows Remote and its fixed restart actuator:

```powershell
.\bin\hashi_remote_ctl.ps1 install
.\bin\hashi_remote_ctl.ps1 start
.\bin\hashi_remote_ctl.ps1 status
```

The Windows supervisor resolves the instance identity while it still has access
to instance configuration, then persists the instance id, display name and
Backend API port in the scheduled-task command. The network-facing Remote task
stays `Limited`. A separate deterministic `HashiRestart-<instance>` task runs
`Highest`, accepts no caller-supplied command, and can only invoke that
instance's fixed restart controller. It stops the old Core and triggers a
separate `HashiRuntime-<instance>` task that provides an isolated elevated
launch boundary for the replacement Core. The restart actuator therefore exits
after readiness and remains reusable while Remote stays online. This bridges a
privilege difference without granting the Remote process broad elevated access. The Limited task
principal is not expected to read protected `agents.json`; failure to do so
must never silently advertise the generic `HASHI` identity or default Backend
API port.

Both fixed tasks explicitly delegate read access to their runtime principal.
Elevated registration and `ensure` repair this delegation even when the
existing task definitions already match, preserving the owner and other
access entries without granting task-edit rights. An administrator seeing a
task does not prove that Limited Remote can see or invoke it. Deployment
acceptance must run the actual `/restart` command through that Remote and
verify the terminal receipt and replacement processes.

Remote's Windows controller helpers, new task registrations and legacy task
runner all disable Python bytecode writes. Starting or refreshing Remote must
preserve the qualified interpreter's sealed file closure.

Linux/WSL supervised Remote:

```bash
bin/hashi-remote-ctl.sh install
bin/hashi-remote-ctl.sh start
bin/hashi-remote-ctl.sh status
```

A child Remote is a valid provider while it is running and has the same trusted
capabilities. On Windows it triggers the fixed elevated actuator when present;
for a same-privilege manual Core, the fixed controller is used directly. The
provider mode is diagnostic context, not an authorization gate.

## Local acceptance test

Use two supported instances, for example `HASHI1` and `HASHI2`.

1. Start both HASHI cores.
2. Start both Remotes (child or supervised) with their normal terminal policy.
3. Complete the normal Hashi Remote handshake and confirm both peers show the
   accepted state.
4. From HASHI1 run:

```text
/restart HASHI2
```

5. Confirm HASHI2 Remote and, on Windows, its exact fixed actuator remain
   available while HASHI2 Core is stopped and started again.
6. Confirm HASHI2 Backend API returns healthy after restart.
7. Inspect HASHI2 `logs/remote_rescue_audit.jsonl` and restart logs.
8. Repeat in the reverse direction.
9. Negative-test by removing trust or lowering the target to `L2_WRITE`; the
   peer `/restart` request must be rejected before a destructive POST.

## Terminal verification

Launch acceptance is not restart success. The durable terminal receipt must
show that the old PID exited, a different live Core PID appeared, the instance
identity matches, the stable Core runtime contract still matches, and the new
process published a Function generation.

The bounded cold-start verification window is ten minutes. Independent-runtime
preparation plus multi-Agent startup can exceed four minutes even when the
replacement is healthy. Windows registration/ensure also upgrades an existing
restart task's execution limit without replacing its principal. A longer wait
never substitutes for the required PID, readiness and frozen-runtime evidence.

Cold restart is also an adoption boundary. For an independent-Core installation,
Remote qualifies the installer-selected destination before stopping the live
process and freezes that contract in the receipt. Terminal verification uses
that admitted destination, while `status_before` retains the old runtime.
An approved Core adoption can therefore succeed; an unqualified target fails
before cutover, and an unexpected Core digest still fails verification.
Dependency and Function generation changes remain recorded, and the restarted
Core enforces its active dependency contract before publishing health.

Backend API health may be `degraded` when an optional connector such as
Telegram or Remote/HChat is unavailable. If local services are ready and no
Agent failed to start, the cold restart is complete. Workbench is the primary
remote frontend; optional Telegram connectivity is not a restart condition.

The completion notice reports only the verified instance restart. Connector
snapshots remain in the durable receipt and live health diagnostics; they are
not appended to success notices, including notices from older Remote peers.
Acceptance wording is channel-neutral and promises a completion notification,
rather than instructing Telegram users to reconnect Workbench. A genuine
terminal failure notice still includes the failed stage and actionable
verification reasons instead of a generic retry.

## Compatibility

Older Remote versions that do not advertise `rescue_restart` are treated as
unsupported, not as broken peers. The existing WatchTower path remains the
fallback for local restart where configured.
