# HASHI3 Runtime and Device-Control Closeout — 2026-09-05

Status: **implemented and live-adopted on HASHI3; the runtime-alignment
follow-up is offline-green and active in the WSL Core/Function Worker;
operator-dependent canaries remain explicitly open**

Scope: HASHI3 only. HASHI1 and HASHI2 were not changed, restarted, messaged, or
used as fallback endpoints during this work.

This document supersedes the live-status boundary in
[`HASHI3_FUNCTION_WORKER_PAUSE_CHECKPOINT_2026-09-04.md`](HASHI3_FUNCTION_WORKER_PAUSE_CHECKPOINT_2026-09-04.md).
It is a HASHI3 pilot closeout, not approval to promote the implementation to
another instance.

## Implemented mechanisms

- Core API 2, Function API 2, per-Agent Function Worker processes, Worker
  protocol 1, and generation schema 2 are active.
- `/reboot` verifies an immutable artifact, drains only the selected routes,
  atomically swaps Worker pointers, and leaves Core-owned services alive.
- Cold start and hot `/reboot` now use the same explicit Function roots. The
  hot path no longer seeds a generation from the timing-dependent set of
  modules already loaded in Core.
- Unexpected Function Worker exit recovers from the same immutable generation.
- Workbench and API Gateway publish instance-scoped endpoint receipts. Runtime,
  Remote routing, health, and Worker bootstrap consumers verify the exact
  HASHI3 identity instead of relying on a fixed address or port.
- System-exchange requests carry typed origin/kind/hop metadata. Replies are
  terminal, system-exchange tool permissions are fail-closed, duplicate
  conversation traffic is deduplicated, and HChat/protocol send paths are
  unavailable from the restricted exchange context.
- Core owns a versioned Capability Broker with authenticated registration,
  health revalidation, expiry, replay protection, task/device/session leases,
  explicit Browser-to-Computer handoff, cancellation cleanup, and audit.
- Windows Computer and Browser Workers are separate persistent `pythonw.exe`
  tasks. Routine Computer actions force the native Win32 provider and cannot
  silently fall back to a per-action PowerShell, CLI, MCP, `uv`, or npm child.
- A WSL Core publishes its current Windows host-gateway candidates from the
  live routing table. A Windows Worker started with `--host auto` selects only
  a locally bindable candidate, waits silently if Core is not ready, and
  rebinds when the published gateway changes.
- Device path conversion verifies authorized roots and supports Windows/WSL,
  spaces, Unicode, OneDrive, and shared artifacts without interpreting a WSL
  path as a native Windows path.
- Browser Bridge installation is instance-scoped: extension identity, native
  host name, install directory, named pipe, authentication file, launcher, and
  registry entry are unique to HASHI3.
- HASHI API transport now preserves complete request, response, and SSE audit
  evidence with credential headers masked. Provider failures retain typed
  diagnostics and fail closed without the obsolete `/verbose off` advice.
- Typed Scheduler runs are isolated from ordinary chat history, Memory+ and
  fixed Provider sessions. Scheduler read-back receives the currently
  published instance endpoint; no HASHI1 address or fixed port is embedded.
- Pre-Turn Compact usage is durably bound to the correct HER Session, legacy
  Flex policy migrates once without overwriting later choices, and interrupted
  Session Runs converge per Agent after Worker replacement.
- HChat always uses one Direct turn with explicit caller Session/chat binding;
  `/say` reads only a confirmed delivered assistant reply from the current
  route; Codex failures retain terminal, event-log and side-effect/replay facts.
- Telegram flood-control recovery releases at the recorded deadline, retries
  recovery notices, and keeps stale incidents isolated. `/verbose`, `/think`
  and `/commentary` affect the active turn without replaying hidden events;
  `/meter` includes total and HER stage timing plus localized cache savings.
- `/model` presents Direct, Strategy, Planning and Execution controls. Native
  Voice performs a complete capability check and fails closed rather than
  silently selecting an unsupported fallback.
- Every Session owns Workzone slots `main` and `1` through `9`. Active roots
  are passed exactly to backends and tools, never widened to a common parent;
  revisions reject stale menus/path replies and only enabled slots enter the
  protected PCM runtime context.
- Each Function Worker owns a dedicated provider-interrupt thread. Core-owned
  Telegram/Workbench command ingress reaches stop/steer/focus/retry through the
  versioned Worker RPC, while orderly cleanup remains on the Worker event loop.
  HASHI Remote source also supports authenticated restart and supervised hot
  reboot through the token-protected Workbench admin command endpoint, with an
  audited hard-restart fallback.

## Final live receipt

Observed at 2026-09-05 13:20 AEST after the runtime-alignment cold adoption and
same-generation hot cutover:

```text
instance_id              HASHI3
Core PID                 645506
Core API                 2
Function API             2
Worker model             per-agent-process
Worker protocol          1
generation schema        2
active Agent             agent1
Function Worker PID      645883
Function phase           ACTIVE / accepting
Function generation      sha256:af3ab1eae9280713105767d55797fcab2f367bf18168f6091b95df9a4b629026
Function module count    261
Workbench                healthy, dynamically published on port 18804
API Gateway              healthy and accepting on port 18805
Computer Worker PID      8268
Computer capability      registered / authenticated_http_host_gateway
Computer task            Running
Browser task             Ready, deliberately not started
active device leases     0
```

The observed Computer endpoint used the current WSL host gateway and an
ephemeral port. Those values are receipts, not configuration constants. Its
heartbeat expiry advanced repeatedly, proving continued Core-to-Windows health
revalidation rather than a one-time registration.

A final read-only recheck at 2026-09-05 13:42 AEST confirmed the same Core and
Function Worker PIDs, generation, 261-module manifest, healthy Workbench/API
Gateway, Computer Worker PID `8268`, and zero active leases. Windows Task
Scheduler still reported Computer `Running` and Browser `Ready`; no HASHI3
Remote process was deployed or started.

## Live acceptance completed

- Controlled cold adoption replaced the Phase 1 Core without touching any
  Remote process or another HASHI instance.
- A direct local `/reboot min` replaced only `agent1` while Core, Workbench,
  API Gateway, and the Computer Worker retained their PIDs and health.
- A targeted termination of the exact active Function Worker PID recovered a
  new Worker on the same immutable generation while Core stayed healthy.
- A cold Core restart removed and rotated the Capability bootstrap token. The
  already-running Windows Worker retained its PID and automatically
  re-registered after the new Core became ready.
- In the earlier device-control baseline, cold start and subsequent hot
  `/reboot min` both produced the same generation ID and 258-module manifest;
  only the Worker PID changed. The later runtime-alignment adoption superseded
  that baseline with the 261-module generation recorded above.
- `/usecomputer status` reported the registered Computer Worker from the Core
  registry. `/browser status` truthfully reported the logged-in extension
  bridge unavailable.
- The Windows native desktop probe reported an interactive `Default` desktop,
  unlocked state, and three displays. Locked or unavailable desktops fail
  closed at Worker health and action time.
- The HASHI3 Browser native host was installed with namespace
  `hashi3-8b78703a`, extension ID
  `lnndajclehjnhaecnoakfpocmbhegodn`, and a matching allowed origin. No other
  instance's Chrome registration was modified.
- The alignment candidate independently reached READY before taking traffic.
  An attempted targeted cutover against the earlier Core was rejected before
  cutover because its protected-Core digest differed; PID `563831` stayed
  active and services remained healthy. This proves the Core migration guard
  did not misreport a Function-only change.
- A controlled HASHI3-only cold migration then activated the exact READY
  generation in Worker PID `645654`. A subsequent `/reboot min` reproduced the
  same generation and atomically replaced only that Worker with PID `645883`.
  Core PID `645506`, Computer Worker PID `8268`, Workbench and API Gateway all
  remained healthy, and active device leases stayed at zero.

No HChat, Remote protocol message, system exchange, model prompt, Telegram
message, or live cross-instance UAT was sent during this closeout.

## Verification receipts

```text
Complete offline product suite
3424 passed, 6 skipped, 59 deselected, 26 warnings

Curated Core gate
544 passed, 1 warning

Contract scope
45 passed, 1 skipped, 3443 deselected

Platform scope available from WSL
3 passed, 11 skipped, 3475 deselected

Function/Core generation and protocol boundary
74 passed

Runtime-alignment control/Remote recovery focus
37 passed

Candidate Worker READY probe
READY, generation sha256:af3ab1eae9280713105767d55797fcab2f367bf18168f6091b95df9a4b629026

Final native-Windows device/identity/installer suite
53 passed

PowerShell AST validation
4 scripts passed

Ruff on all changed and untracked Python paths
passed

git diff --check
passed; two existing Windows line-ending notices only
```

The Windows suite includes 50 mixed persistent actions and asserts that no
PowerShell, CLI, MCP, `uv`, npm, or per-action Python child is launched.

## Installed HASHI3 browser files

```text
%LOCALAPPDATA%\HASHI\browser_bridge\<instance-key>\extension
%LOCALAPPDATA%\HASHI\browser_bridge\<instance-key>\hashi_browser_bridge_host.exe
```

The matching uninstallers are instance-scoped and remove only the HASHI3 task,
state receipt, registry entry, and install directory they own.

## Explicitly open operator-dependent gates

These are not code TODOs and are not recorded as successes:

1. Chrome's Default profile does not yet contain extension
   `lnndajclehjnhaecnoakfpocmbhegodn`. Chrome requires an operator to enable
   Developer mode and load the unpacked HASHI3 extension. Until then the named
   pipe is absent, the Browser task remains stopped, and a real logged-in
   upload/handoff canary cannot run.
2. The live HASHI3 configuration contains one Agent only. Multi-Agent cutover,
   lease exclusion, broad atomic activation, failure rollback, and recovery
   exhaustion passed isolated tests, but a real two-Agent instance canary needs
   an approved second Agent configuration.
3. The configured Telegram token is a placeholder. Real Telegram offset and
   delivery canaries need an authorized test bot. No production or personal
   chat was used for validation.
4. The active Core deployment is WSL. Native-Windows components passed their
   Windows test suite, but a full native-Windows Core launch remains a separate
   deployment canary.
5. Logoff, desktop lock, RDP transition, live DPI/display changes, and physical
   click/key stress are intentionally not forced on the operator's active
   desktop without a scheduled disruptive-test window.
6. The HASHI3 Remote side program is not currently deployed. Its authenticated
   restart/reboot source and offline tests are complete, but a real independent
   recovery exercise requires an explicitly authorized Remote deployment with
   `L3_RESTART`; no HASHI1 or HASHI2 Remote process may be used as a substitute.

Until those gates are explicitly authorized and observed, the accurate release
statement is: **HASHI3's runtime alignment and device-control mechanisms are
implemented, offline-green, and live-adopted for the WSL Core, isolated
Function Worker and Windows Computer Worker; the logged-in Browser path,
HASHI3 Remote deployment and disruptive/external canaries remain unclaimed,
and promotion to HASHI1/HASHI2 is not approved.**
