# Plan-Risk Review: sl-20260516-231815-hashi9-restart-debug

**Reviewer:** akane@HASHI1  
**Date:** 2026-05-17  
**Loop:** sl-20260516-231815-hashi9-restart-debug  
**Step:** step-004 (pre-implementation plan review)

---

## Current State (as of review)

The debug loop itself triggered a controlled smoke restart at 09:22 UTC. That smoke
has changed the system state materially:

| Item | State |
|---|---|
| HASHI9 workbench (port 18819) | **OFFLINE** — stopped by controlled smoke, not restarted |
| HASHI9 remote listener (port 35821) | **ALIVE** — still announcing to WatchTower at 09:25 |
| WatchTower service | Running (09:08 start, active) |
| WatchTower restart record | Present: `restart-25749a00e09548439410f92220755f88.json`, phase `failed_verify` |
| Audit log | Entry recorded: `outcome: restart_failed_verify` at 23:23:40Z |
| Start/stop capture logs | Both 0 bytes (`remote_rescue_hashi_start.log`, `remote_rescue_hashi_restart.log`) |

**Immediate precondition**: HASHI9 must be manually restored before any further testing.
The controlled smoke stopped HASHI9's workbench and the automated restart failed.

---

## Two Distinct Problems

The evidence separates the original failure from the controlled smoke failure. These
must be diagnosed independently.

### Problem A — Original `/restart` left no WatchTower evidence

At 09:13 AEST (before the debug loop), hashiko@HASHI9 triggered a Telegram `/restart`
attempt. WatchTower shows zero evidence: no `state/restarts/` entry, no audit log
entry, no stdout stop/start/verify activity.

### Problem B — Controlled smoke stop succeeded but start failed

The debug loop's controlled smoke at 09:22 UTC DID reach WatchTower (restart record
and audit entry exist). Stop phase succeeded (pid 24512 killed, returncode 0). Start
phase launched powershell pid 10280, but HASHI9 did not become healthy within 20
seconds (`failed_verify`). The start capture log is 0 bytes.

---

## Hypothesis Ranking

### Problem A hypotheses

**A1 — MOST LIKELY: HASHI9 was running old code without `/restart` registered**

The checkpoint 2 commit added `/restart` to `orchestrator/commands/api_restart.py` and
the runtime command registry auto-discovery. If HASHI9 was running an older Python
process (started before the commit was deployed), it would have no `/restart` handler.
The bot would silently discard the `/restart` command (no registered handler, no
callback route). WatchTower would never be contacted. This explains zero WatchTower
evidence perfectly.

Evidence supporting: WatchTower started fresh at 09:08 (just before the debug loop),
suggesting the Windows environment was recently restarted. HASHI9's process at 09:13
may have been started from a pre-checkpoint-2 state.

Read-only probe: `git -C /mnt/c/Users/thene/projects/HASHI log --oneline -5` at the
time the 09:13 process was running. Check if the hashiko process start time predates
the checkpoint 2 commit (commit `51e6e65` or equivalent).

**A2 — LIKELY: `hardrestart:confirm` failed closed before dispatch**

If the `/restart` command WAS registered but WatchTower was not reachable (wrong
address, auth failure, or not yet running at 09:08 startup), `restart_command` would
show an error panel. The user may have clicked `Hard Restart → Confirm`, but
`_watchtower_restart_available()` re-checks status and fails closed. No dispatch, no
WatchTower record.

Evidence supporting: WatchTower only started at 09:08, only ~5 minutes before the 09:13
attempt. It's possible HASHI9's first status probe hit WatchTower before it finished
startup and got a failure.

Read-only probe: inspect hashiko Telegram conversation log around 09:13 for the exact
message sequence (was the `/restart` status panel shown? was the error path shown? did
the confirmation flow complete?).

**A3 — POSSIBLE: Wrong WatchTower address in HASHI9's instances.json**

`WATCHTOWER_INSTANCE = "WATCHTOWER"` in `api_restart.py` resolves via
`_candidate_base_urls("WATCHTOWER")` → `_instance_entry("WATCHTOWER")` →
`instances.json`. If the registered WatchTower address was wrong (e.g., stale IP from
a previous network state), all HTTP probes would fail without WatchTower recording
anything.

Evidence supporting: WatchTower peer registry shows multiple IP addresses
(192.168.0.211, 192.168.0.6 for INTEL-WT). The `instances.json` `watchtower` entry
may have had a stale address at 09:13 that was corrected by WatchTower's own peer
discovery by 09:22 (when the controlled smoke succeeded).

Read-only probe: check `instances.json` watchtower entry and compare against WatchTower
startup log (`Peer port: 43766`) to confirm the address that would have been used.

---

### Problem B hypotheses

**B1 — MOST LIKELY: HASHI9 remote listener survived the stop**

WatchTower peer announcements show HASHI9 at 192.168.0.211:35821 still active at 09:25,
after the workbench stop at 09:23. `bridge_ctl.ps1 -Action stop -Quiet` killed the
workbench process (pid 24512, confirmed by `returncode: 0` and `pid_file_exists: false`),
but did NOT kill the remote listener (port 35821).

When `bridge_ctl.ps1 -Action start -Resume` then ran, it may have detected an existing
bridge-u process (the remote listener) and either refused to start or started in a
conflicting state. The workbench at 18819 was never relaunched.

This is consistent with the 0-byte start log: `Write-Log` in bridge_ctl.ps1 writes
to its own file (not stdout), so empty stdout is expected. The powershell process did
launch (pid 10280), but the start script may have detected "already running" and exited
without starting the workbench.

Read-only probe: check if `bridge_ctl.ps1 start` detects running processes before
starting. Check if the remote listener (port 35821) is a separate process from the
workbench (PID 24512). Confirm whether stop kills all bridge-u processes or only the
workbench.

**B2 — LIKELY: 20-second verify timeout insufficient after cold start**

Even if `bridge_ctl.ps1 -Action start -Resume` did launch a new HASHI9 workbench, a
cold start (model loading, Telegram reconnection, workbench startup) may take longer
than 20 seconds. WatchTower would declare `failed_verify` while HASHI9 is still
initializing.

Evidence: HASHI9 with API Gateway enabled (as per status_before) has more startup work.
The original HASHI9 process ran for a long time (started 07:46), suggesting it took
time to stabilize.

This is NOT a blocker for the `/restart` feature correctness (WatchTower correctly
reports the verify failure), but it is a correctness gap if HASHI9 actually does start
successfully 30+ seconds later.

**B3 — POSSIBLE: DETACHED_PROCESS flag prevents stdout capture but not process success**

The `_start_hashi_process()` opens a log_handle and passes it as stdout to a
DETACHED_PROCESS powershell. The `finally: log_handle.close()` closes the Python side
immediately after Popen returns. Even if the child process inherits the handle, if
`Write-Log` in bridge_ctl.ps1 writes to its own log file (not stdout), the captured
log will always be empty regardless of success.

This is a logging deficiency, not a functional failure. It means start failures are
invisible to WatchTower unless they throw an exception.

Read-only probe: check `bridge_ctl.ps1` for what `Write-Log` writes to (stdout vs.
file). Check if there is a `bridge_ctl.log` or similar file in the HASHI9 bin directory
with evidence of what happened during the 09:23 start attempt.

---

## Evidence Gaps

| Gap | Why it matters | How to fill |
|---|---|---|
| HASHI9's exact commit at 09:13 | Determines if A1 (old code) is confirmed | `git -C /mnt/c/.../HASHI log --oneline -5` and check process start metadata |
| Telegram message sequence at 09:13 | Determines if confirmation panel was shown | Read hashiko conversation log or Telegram history |
| Whether HASHI9 remote listener survived stop | Determines B1 | `netstat -ano | findstr :35821` on Windows right now |
| `bridge_ctl.ps1` Write-Log destination | Determines if B3 is a symptom or cause | Read bridge_ctl.ps1 `Write-Log` function definition |
| Whether bridge_ctl.ps1 start checks for existing processes | Core of B1 | Read the start action in bridge_ctl.ps1 |
| HASHI9 workbench port in WatchTower config | Confirm 18819 is correctly resolved | Check WatchTower's `global_config.json` or HASHI9's agent config |

---

## Minimum Evidence Before Second Live Smoke

All of the following must be confirmed before a second live smoke:

1. **HASHI9 online**: workbench at 18819 responding, hashiko processing messages.
   Manual start via `bridge_ctl.ps1 -Action start` or direct launch required.

2. **Root cause A confirmed**: either A1 (old code, now fixed by deploying checkpoint 2)
   or A2 (WatchTower address/availability issue at 09:13). At least one hypothesis
   must be confirmed and addressed.

3. **B1 investigated**: determine whether `bridge_ctl.ps1 -Action stop` kills the remote
   listener. If it doesn't, the controlled smoke will fail again for the same reason.
   Fix or workaround required before retry.

4. **B2 assessed**: either confirm the 20-second timeout is sufficient (by timing a
   manual bridge_ctl.ps1 start) or document the expected startup time and note the
   WatchTower verify window needs extension.

5. **Bridge_ctl.ps1 start tested manually**: run `bridge_ctl.ps1 -Action start -Resume`
   from the WatchTower process context (or equivalent non-interactive session) and
   confirm HASHI9 comes up healthy. If it doesn't, root cause B1/B3 needs a fix first.

6. **Bounded smoke plan documented**: before the second live smoke, the expected
   evidence must be stated: which restart_id will be created, which audit entry will
   appear, what HASHI9 start time must change.

---

## Risk Flags

**RF-1 BLOCKER**: HASHI9 is currently offline. Any further live testing is blocked
until manual recovery. The controlled smoke left HASHI9 in a stopped state. Do not
attempt another WatchTower restart until B1 is understood — it will fail again for
the same reason.

**RF-2 NON-BLOCKER**: The start/stop capture logs (0 bytes) provide no visibility into
what `bridge_ctl.ps1` does when run by WatchTower. This is a logging deficiency that
should be fixed regardless of Problem B's outcome, but it does not block acceptance.
Fix: `bridge_ctl.ps1` should use a dedicated log file (not stdout redirect) and
WatchTower should read from that file path to get start evidence.

**RF-3 NON-BLOCKER**: `_start_hashi_process()` closes `log_handle` in a `finally`
block immediately after Popen, before the detached process has written anything. On
Windows, the `DETACHED_PROCESS` flag may prevent handle inheritance entirely. The
current log file will always be empty. This should be addressed but does not affect
restart correctness.

**RF-4 ADVISORY**: The 20-second verify deadline in the restart endpoint is hardcoded.
If HASHI9's cold-start time varies (model loading, Telegram reconnect), this may
produce false `failed_verify` outcomes when HASHI9 actually does start successfully
a few seconds later. Consider either increasing the deadline or making it configurable.

---

## Plan Verdict

**Pre-implementation review: CONDITIONAL PASS with two blockers.**

The debug plan is sound in structure (read-only probes → hypothesis ranking → bounded
fix → controlled smoke). However, two conditions must be met before any fix is
implemented and retried:

**BLOCKER P1**: Restore HASHI9 manually and confirm it is running before any further
action. All subsequent steps depend on a live HASHI9.

**BLOCKER P2**: Determine conclusively whether `bridge_ctl.ps1 -Action stop` kills
the HASHI9 remote listener (port 35821). If it does not, the WatchTower restart loop
will always fail at the verify phase because `bridge_ctl.ps1 -Action start` will
detect a running process and refuse to start (or start a conflicting workbench). This
must be fixed or confirmed before a second smoke attempt.

Once both blockers are resolved, the fix scope is narrow:

- If A1 confirmed: ensure checkpoint 2 code is running on HASHI9.
- If B1 confirmed: fix `bridge_ctl.ps1 -Action stop` to kill all bridge-u processes,
  or adjust WatchTower's `_stop_hashi_process` to wait for port 35821 to close.
- If B2 confirmed: extend the verify deadline in `_hashi_control_restart` or tune the
  startup script to front-load health readiness.

No other files should be in scope for this fix.
