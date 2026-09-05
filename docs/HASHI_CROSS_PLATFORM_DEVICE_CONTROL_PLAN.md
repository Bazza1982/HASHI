# HASHI Cross-Platform Device Control Plan

Status: **implemented on HASHI3; Computer Worker live-accepted; logged-in
Browser and disruptive/external canaries remain open**

Decision date: 2026-09-04

Scope: HASHI3 only until its implementation and live acceptance gates pass.
Do not patch HASHI1 or HASHI2 from this plan.

## Implementation update — 2026-09-05

The architecture below is now implemented in HASHI3. Core capability
registration, identity checks, expiry, leases, handoff, cleanup, endpoint
discovery, status, audit, persistent Windows Workers, native-only Computer
execution, instance-scoped Browser installation, and uninstall paths are in
place. A WSL Core and the real Windows Computer Worker passed live registration,
heartbeat, Function Worker cutover/crash isolation, and Core-restart recovery.

The complete receipts and exact acceptance boundary are in
[`HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).
Chrome has not loaded the new unpacked HASHI3 extension, so the logged-in
Browser Worker, real upload handoff, native-Windows Core deployment, real
two-Agent configuration, and disruptive desktop transitions remain explicit
operator-dependent gates. No promotion to HASHI1 or HASHI2 is approved.

## Decision

HASHI will present one coordinated computer-control capability while keeping
the two execution authorities isolated:

- a Browser Control Worker for authenticated browser/tab/page operations; and
- a Computer Control Worker for the real desktop, native applications, system
  windows, mouse, keyboard, screenshots, and file pickers.

The workers share discovery, authorization, audit, task context, and control
lease semantics through a Capability Broker. They do not share a process or
silently inherit each other's permissions.

The design must work whether HASHI Core runs in WSL or native Windows. Routine
actions must travel over persistent IPC to already-running workers. They must
not launch a visible PowerShell, Command Prompt, Windows Terminal, Python, uv,
or npm console for every screenshot, click, key press, or browser action.

```text
Agent Function Worker
        |
        | stable Core RPC
        v
Capability Broker / policy and lease authority
        |
        +---- Browser Control Worker
        |       +---- browser extension / native messaging adapter
        |
        +---- Computer Control Worker
                +---- Windows interactive desktop adapter
```

## Architectural boundaries

### Stable HASHI Core

Core owns the control-plane facts that must survive an Agent Function Worker
replacement:

- capability registration and health;
- Agent, task, instance, device, user-session, and request identity;
- authorization and explicit privilege escalation;
- lease issuance, handoff, cancellation, timeout, and audit correlation;
- route selection and the policy that chooses browser or computer control; and
- stable RPC exposed to replaceable per-Agent Function Workers.

No Function Worker may hold an untracked direct connection that bypasses the
Broker's authorization, lease, or audit boundary.

### Browser Control Worker

The Browser Control Worker owns browser-scoped execution and the existing
extension/native-messaging integration. It should be preferred for page text,
DOM-aware clicks, form input, authenticated tabs, and browser screenshots.

Its authority stops at the browser boundary. It does not gain desktop-wide
mouse, keyboard, window, or filesystem-dialog control merely because a browser
operation fails.

### Computer Control Worker

The Computer Control Worker is a Windows-native process in the signed-in
interactive user's session. It owns desktop screenshots, display metadata,
window enumeration/focus, native UI automation, and physical mouse/keyboard
injection.

It must remain outside HASHI Core and outside each Function Worker. A crash,
upgrade, or dependency failure in desktop automation must not restart Core or
replace an unrelated Agent generation.

Windows Services run in Session 0 and cannot reliably operate the signed-in
user's desktop. Therefore the normal desktop worker must be a per-user,
interactive-session background agent started at sign-in, not a Session 0
service. A service may supervise metadata or installation, but it must not own
interactive input.

## WSL and Windows operating contract

The target UI determines worker placement; the OS hosting HASHI Core does not.

| Core location | Target | Required execution placement |
|---|---|---|
| WSL | Windows Chrome or Windows application | Windows user-session worker |
| Native Windows | Windows Chrome or Windows application | Windows user-session worker |
| WSL | Linux/WSL browser or virtual desktop | WSL/Linux worker with an isolated Unix socket |
| Native Windows or WSL | A different authorized device | Remote device worker over an authenticated transport |

All protocol messages must use platform-neutral action schemas. OS-specific
code stays in the platform worker. In particular:

- WSL code must not pretend that Win32 UI APIs are locally available;
- Windows workers must not interpret Bash commands or WSL paths as native
  Windows commands or paths;
- file/path handoff must use an explicit path resolver that returns verified
  Windows and WSL representations when the same file is shared;
- screenshots and other artifacts must be streamed or written to a negotiated
  shared artifact location, not an assumed checkout path; and
- capability health must report the real OS, desktop session, display state,
  protocol version, and supported action set.

Locking, logoff, Remote Desktop session changes, display changes, and WSL
restart are capability-state changes. They must make the affected worker
unavailable or degraded without making HASHI Core unhealthy.

## Dynamic discovery and endpoint rules

No Browser Worker, Computer Worker, Function Worker, health path, recovery
path, or test may embed a particular address or port.

Each live registration must include at least:

```text
capability_id
capability_kind
instance_id
device_id
user_session_id
platform
transport_kind
negotiated_endpoint
protocol_version
supported_actions
worker_pid_and_generation
authorization_key_id
health_and_expiry
```

Required discovery behavior:

1. A worker binds an OS-appropriate endpoint, allowing an ephemeral port when
   TCP is used, and registers the actual endpoint.
2. Broker bootstrap information comes from canonical instance configuration or
   authenticated discovery, never a source-code constant.
3. Windows-local transport should prefer a secured named pipe or authenticated
   loopback channel; WSL-local transport should prefer a permission-restricted
   Unix socket.
4. A WSL/Windows boundary may use authenticated loopback or host-gateway
   candidates, but reachability alone never establishes identity.
5. Every connection performs a protocol and identity handshake. The response
   must match the expected instance, device, user session, capability, and
   authorization scope before the route becomes usable.
6. Registrations expire, health is revalidated, and stale endpoints are
   removed. A port or address change must be rediscovered without a code edit.
7. Multi-instance routing fails closed. A healthy HASHI1/HASHI2 endpoint must
   never satisfy a HASHI3 capability check.

This work should reuse the canonical configuration/discovery primitives from
the deferred Workbench endpoint repair where they fit. It must not create a
second hard-coded endpoint registry or assume the Workbench port is the device
worker port.

## No-popup background lifecycle

The steady-state contract is stronger than merely passing `-NoNewWindow`:

- the Windows workers start once per signed-in user session and keep a
  persistent authenticated channel;
- routine actions are RPC messages and do not spawn PowerShell, `cmd.exe`,
  Windows Terminal, `uv run`, Python console executables, or npm launchers;
- the installed worker uses a windowless packaged executable or `pythonw.exe`
  with stdout/stderr redirected to bounded rotating logs;
- any unavoidable child process is created with Windows no-window creation
  flags and hidden startup settings, with its output captured;
- the browser native-messaging launcher is also windowless;
- crash recovery uses a silent per-user supervisor with bounded backoff and no
  restart loop; and
- status, diagnostics, upgrades, and failures appear in Workbench and HASHI
  commands/logs rather than flashing terminal windows.

One-time installation or an administrator-authorized repair may deliberately
show a consent or setup window. That is not permission for ordinary control
actions to create visible consoles.

The existing per-action PowerShell/CLI path remains only a temporary,
explicitly selected diagnostic fallback during migration. It is not the target
runtime and must not be automatic once the persistent worker is accepted.

## Routing, leases, and safe handoff

Browser control remains the least-privileged default for work inside a web
page. Computer control is selected for native applications, system dialogs,
browser chrome, file pickers, or operations that genuinely require the desktop.

The following rules are mandatory:

- observation may run concurrently only when the provider can prove it has no
  input or focus side effect;
- clicks, typing, scrolling, focus, navigation, and other mutations require a
  short-lived control lease for the affected device/session/window;
- the platform-side agent enforces the final device write lock so two HASHI
  instances cannot concurrently drive the same desktop;
- a browser-to-computer handoff records the source capability, target
  capability, window/tab identity, task identity, and lease transition;
- a Browser Worker failure must not silently escalate to full-computer control;
  policy or user authorization must permit the escalation; and
- timeout, cancellation, Agent reboot, Worker crash, or task completion releases
  the lease and performs best-effort input-state cleanup.

Representative handoff:

```text
Browser Worker clicks Upload
    -> browser lease is yielded
    -> Computer Worker receives the file-dialog/window lease
    -> Computer Worker selects the file and yields
    -> Broker verifies the browser target and returns its lease
```

## Security and observability

- Bind locally by default and use per-instance/per-user credentials with
  rotation and least-privilege scopes.
- Never expose desktop control merely because a port is reachable.
- Reject replayed, expired, incorrectly scoped, or wrong-instance requests.
- Redact typed secrets and sensitive screenshot data from ordinary logs.
- Audit route choice, authorization decision, lease, target identity, action,
  result, duration, and handoff without logging raw credentials.
- Provide `/browser status`, `/usecomputer status`, and Workbench views from the
  same canonical capability registry.
- Report whether a failure is discovery, authentication, locked desktop,
  unsupported action, lease conflict, worker health, or execution failure.

## Implementation sequence on HASHI3

1. Inventory current Browser Bridge, `windows_use`, Windows helper, native
   messaging, path conversion, startup, and endpoint assumptions. Record the
   current per-action process-spawn behavior before replacing it.
2. Complete the deferred canonical endpoint/configuration mechanism needed by
   Workbench and capability bootstrap, including non-default endpoint and
   cross-instance tests.
3. Define versioned capability registration, health, action, artifact, lease,
   cancellation, and handoff schemas across the Core/Function boundary.
4. Add the stable Core Capability Broker and a platform-side device-session
   lease authority. Keep all UI libraries and provider dependencies outside
   Core.
5. Package the Windows Computer Control Worker as a persistent, silent per-user
   background agent. Migrate the default `windows_*` path away from per-action
   PowerShell/uv/npm spawning.
6. Adapt the existing Browser Bridge behind its own Worker registration and
   remove fixed pipe/socket/port assumptions from runtime routing while
   preserving supported actions.
7. Implement explicit route selection and Browser-to-Computer handoff without
   automatic privilege escalation.
8. Add status, health, audit, bounded logs, silent recovery, installation,
   upgrade, and uninstall behavior.
9. Run the acceptance matrix below on HASHI3. Do not promote or copy the
   mechanism to HASHI1/HASHI2 until the evidence is reviewed and approved.

## Acceptance gates

The plan is not complete until HASHI3 proves all of the following:

### Cross-platform and discovery

- WSL Core controls the real Windows desktop and authenticated Windows Chrome.
- Native Windows Core performs the same representative tasks.
- Non-default and dynamically changed endpoints recover through discovery.
- Stale, spoofed, wrong-session, and wrong-instance registrations are rejected.
- HASHI1 and HASHI2 remain untouched and cannot answer a HASHI3 capability
  health check.
- Windows/WSL path and artifact handoffs work for spaces, Unicode, OneDrive,
  and files outside the repository within authorized roots.

### Silent operation

- Sign-in startup and worker recovery create no visible terminal window.
- At least 50 mixed screenshots, clicks, key presses, window actions, and
  browser actions create zero visible console windows.
- The same run proves actions use the existing persistent worker rather than a
  new PowerShell/uv/npm/Python process per action.
- Logs remain bounded and actionable when stdout/stderr is unavailable.

### Isolation and handoff

- Browser Worker crash/restart does not interrupt Computer Worker, Core, or an
  unrelated Agent Function Worker; the reverse case also passes.
- A two-Agent test proves lease exclusion and correct cancellation/release.
- A real browser upload flow hands control to a Windows file picker and back
  without concurrent writes or loss of target identity.
- Locked desktop, logoff, display/DPI change, helper crash, WSL restart, timeout,
  and input cleanup have explicit, truthful outcomes.
- A failed browser action never upgrades itself to desktop-wide authority
  without the required policy or user approval.

## Current boundary

The mechanism is implemented and live-adopted on HASHI3 for a WSL Core and the
Windows Computer Worker. The Browser native host and stopped Browser Worker
task are installed, but Chrome's Default profile has not loaded the
instance-scoped extension, so Browser registration and real Browser-to-file-
picker handoff are not claimed. Native-Windows Core, real two-Agent, real
Telegram, locked/logoff/RDP/display-change, and physical-input stress canaries
also remain unclaimed until their required configuration or disruptive-test
window is authorized. See the linked closeout for exact evidence.
