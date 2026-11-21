# Manual Desktop v1 — Functions / Windows Worker

Status: Standard and Smooth are adopted on HASHI1; the user reports Smooth looks good.
Ultra Smooth source, running adoption, measured performance and physical acceptance
are tracked separately.
Prior feature baseline: `3004fb1799928506880b7ae3139edf8b96937625`.

Functional owner: Frontend Connectors for the public desktop ingress/projection;
PAO remains the single authority for capability registration and write leases.
Engineering placement: replaceable Functions plus the existing Windows interactive
Computer Worker. No `CORE_SOURCE_PATHS` file is edited. No model/Agent Run is created.

## Enable locally

1. Use the reviewed shared-code checkout and preserve unrelated local changes.
2. No HASHI global desktop switch gates the local entry point: `desktop_enabled`
   defaults and governed deployment profiles cannot remove the local Workbench
   desktop. The Workbench keeps its own explicit opt-out (its settings switch or
   `HASHI_WORKBENCH_DESKTOP=0`), which is a user preference, not a HASHI gate.
3. The existing ignored `secrets.json` MUST contain a nonempty
   `workbench_admin_token` for remote desktop callers. Reuse the existing value,
   or create a unique random value for this instance through revision-safe
   configuration when absent. Local loopback callers are exempt (see "Local
   availability and trust boundary"). Preserve every other secret and do not
   add another pairing system. The authenticated Remote proxy injects this
   token on its loopback Backend API hop.
4. Adopt the updated shared Functions using the instance's established hot adoption
   procedure. These are shared services, not merely one Agent's code. Source pull
   and running-generation adoption are distinct facts.
5. Adopt the updated Remote process separately: its allowlist forwards
   `X-Desktop-Meta`. Remote verifies which local Backend API address belongs to
   this instance before the first desktop request. An uncertain desktop write
   never retries another address. Do not widen the Remote lifecycle scope.
6. Update the Windows Computer Worker code root to this feature checkout. Run it
   once per logged-in Windows user, through the existing installer:

```powershell
.\scripts\install_device_control_workers.ps1 `
  -BridgeHome "<existing instance state directory>" `
  -CodeRoot "<Windows-local feature checkout>" `
  -PythonwExe "<existing Windows environment>\Scripts\pythonw.exe" `
  -ComputerOnly -BindHost 127.0.0.1 -AdvertiseHost 127.0.0.1
```

This example is for a native Windows HASHI on the same machine. Keep verified
existing bind/discovery settings where appropriate; no WSL or Docker is required.
The installer updates/restarts only the selected instance's Computer task. Inspect
that task name and paths first. Pillow must exist in the isolated Windows helper
environment; do not install packages into a running protected Core interpreter.

7. The compatible frontend must enable its own opt-in and protected external entry.
   Anonymous/demo access is not supported. Only the personal profile is implemented
   in v1; governed profiles return `desktop_profile_unsupported` until explicit
   role-aware admission is qualified.

## Local availability and trust boundary

The local Workbench entry is a pure local capability: HASHI must not gate it.
The HASHI-side `desktop_enabled` global switch and the governed-profile gate
were removed, and the Workbench keeps an explicit user opt-out.

Loopback admission is a *host-level* trust boundary, not an OS-session
boundary. On Windows, any process in any Windows user session on the same
machine can connect to a service bound to 127.0.0.1, so loopback reachability
alone does **not** prove that the caller belongs to the interactive user's own
session. The supported deployment assumes a single trusted OS user on this
workstation; under that assumption the token only binds the local entry to a
rotatable HASHI secret, and the loopback exemption removes a false gate rather
than weakening a real one. Remote callers still require the configured admin
token, and a remote hop with no admin token configured is always rejected.
When the Workbench session cannot resolve a user owner, the reserved
`workbench-local` identity is used; the resolved owner is pinned for the
session lifetime and never derived from the client.

### Multi-user hosts

Target refresh and open are pinned to (instance_id, device_id,
user_session_id), so a caller is never silently switched to another machine
or another Windows session; that pinning is correctness, not an authorization
boundary. On a multi-user Windows host, another session's process can still
reach the loopback desktop endpoint and request the interactive session the
worker is bound to. Minimum viable options, none implemented without explicit
approval:

1. Keep the loopback exemption and document the single-trusted-user
   deployment assumption (current behaviour).
2. Restrict loopback admission to callers of the same Windows session
   (named-pipe transport or an impersonation/session check) so processes
   from other sessions are rejected.
3. Require the admin token for loopback callers on hosts explicitly
   configured as multi-user (a deployment gate, not a new default).

## Protocol

`POST /api/v1/desktop/operation`, JSON, <=32 KiB. Loopback callers (the local
Workbench) are admitted without the Backend API admin token; remote callers
require it. A session ID alone grants no authority. Client ID is tab
correlation, not an authentication credential. Operations:

- `targets`: sanitized Windows computer capabilities with all desktop actions.
- `open`: an exact target including worker generation. The server validates the
  target and the worker open BEFORE evicting any session for capacity, so a
  failed open never disrupts an active viewer. Same-device refreshes (a
  restarted worker advancing generation or re-registering its capability) are
  accepted; a requested device that is no longer registered returns
  `desktop_target_changed` instead of silently switching machines. The server
  returns session ID and display metadata. No new OS login session is created.
- `frame`: session ID, optional `after_frame`, and optional `refresh_profile=smooth`
  or `refresh_profile=ultra_smooth`;
  returns `image/jpeg` or HTTP 204
  when unchanged. `X-Desktop-Meta` holds bounded ASCII JSON, frame/view revisions,
  source rectangle, scaled size, cursor, check age and polling interval.
- `view`: validated display, optional normalized crop and small-screen hint.
- `control`: acquire/heartbeat/release. Acquire returns a lease ID; renew/release
  require that same ID. Only one human or Agent may hold the device/session resource.
- `input`: lease ID, strictly ordered sequence and typed event. Exact repeats return
  the saved receipt; changed repeats and sequence gaps fail. Receipts mean injection
  succeeded, not that the target application completed a business operation.
- `close`: releases the matching manual lease and removes the API session.

The Broker uses its existing authenticated worker transport and same resource
lease map. Manual leases contain `actor_type=user`, an authenticated owner and the
separate desktop session; they do not impersonate an Agent. Explicit instance,
device, user session and worker-generation pinning replaces latest-worker selection
only for this new path. Existing tool interfaces remain compatible.

The Worker holds the existing OS file lock for the entire manual-control lease,
including idle gaps; legacy agent mutations attempt that same lock. An independent
watchdog releases only this controller's injected states and expires control after
30 seconds without renewal (one missed heartbeat no longer drops control). A stale cleanup request from an Agent cannot release
someone else's active manual input.

## Bounds and privacy

Each live desktop session has its own bounded in-memory JPEG cache, <=512 KiB per
frame; at most eight API sessions (the least recently touched session is evicted
for the current user instead of refusing, and eviction runs only after the new
open is validated) and 1600x900 pixels (1280x720 small). Worker
sessions without contact expire after 600 seconds and release their cached frames.
Quality starts at 75, with four bounded encoding attempts maximum. GDI captures/scales
the selected monitor/region directly; no screenshot temp files. Capture/encoding
uses the existing request worker thread, not Core/HTTP event loops. Concurrent frame
requests do not start independent capture jobs. The default Standard profile stays
at up to 2 FPS while active and .5 FPS after ten seconds without a changed frame or
remote input. Workbench's Save data choice polls Standard at up to 1 FPS.
The explicit Smooth profile permits up to 20 FPS while active, then backs off to
2 FPS after the same idle period. It is a bounded polling target, not a guaranteed
display rate: capture, encoding and network latency can lower the effective rate.
At the 512 KiB frame ceiling, continuously changing Smooth images can reach about
10 MiB/s of JPEG data, plus transport overhead and greater CPU use. Client requests
remain sequential; busy captures back off, and no requests means no capturing.
Full-resolution 4K frames are never transmitted.

Ultra Smooth is an explicit motion-first choice: up to 30 capture attempts per
second at no more than 1280x720 and 256 KiB per JPEG. The server spaces attempts
by the previous frame size to target at most about 4 MiB/s of JPEG data per
session under sequential polling; Remote/base64 and HTTP overhead add more wire
traffic. It also spaces capture work to target no more than half of one Worker
thread's time. The returned wait subtracts time already spent capturing, so
the budget is measured between capture starts. After 0.5 seconds without a changed frame or input it drops to 10 FPS;
after two seconds it drops to 2 FPS. Hidden Workbench tabs poll at most once per
second. Capture, encoding and network latency can reduce the delivered FPS, so
30 FPS is a ceiling rather than a promise. The existing one-at-a-time capture
guard and session expiry also apply. Smooth remains the clearer 900p option.

Only pending mouse moves may be coalesced by clients. Smooth and Ultra Smooth flush
them at most every 33 ms, versus 100 ms in Standard and Save data. Input validation checks finite
normalized coordinates, display revision, a recently checked frame, sequence and
lease. Releases remain possible with expired frames. Displays are revalidated at
input time. Unicode text normally uses SendInput, never clipboard sync. On the
ordinary desktop, a focused, writable Unicode RichEditD2DPT control instead
receives one undoable EM_REPLACESEL operation: modern Notepad can corrupt accepted
Unicode keyboard packets. The target must still belong to the foreground
thread/process and retain focus immediately before dispatch. Other controls and
the secure desktop retain their existing keyboard paths. A 500 ms native message
timeout is an uncertain edit: it is never replayed or followed by SendInput.
No command/shell/file transfer/audio interfaces are provided. No raw frame or input content is logged.

The ordinary Windows Worker cannot control the login/lock/UAC secure desktop or
higher-integrity processes. An explicit local platform opt-in now selects an
isolated Windows desktop service; the default adapter remains ordinary Windows.
The service is installed by an administrator with
`scripts/install_secure_desktop_host.ps1 -BridgeHome <selected-instance> -Start`.
It binds one verified interactive account and console session, runs a SYSTEM
child in that session, and exposes only bounded screen/input operations through
a local ACL-restricted named pipe. There is no TCP, command, file or credential
interface. ProgramData executable/configuration and their parent directory are
writable only by SYSTEM/administrators. The Worker authenticates the pipe's
kernel owner and session before sending request bytes; the host separately
authenticates the caller's account/session. PAO and the existing OS write lock
continue to own all manual control leases.

The optional host opens the current input desktop per request and restores its
thread desktop afterwards. Held keys/buttons are tracked and reset on an idle
watchdog. Windows credentials are entered by the human, never retrieved or
stored. Secure attention is not synthesized. The installation is session-pinned;
a changed console account/session requires a separately reviewed configuration
adoption, not automatic widening. The installer refuses to overwrite an existing
service or Worker opt-in. Other OS adapters are not implemented.

Input coordinates are fenced by the selected display's geometry. An unrelated
monitor waking or sleeping cannot revoke control; an actual change to the
selected monitor still releases it and clears old frame coordinates. Picture
freshness includes response transit time. Hidden conversation polling pauses
while the Workbench desktop is presented, keeping browser HTTP connections
available for frames, heartbeats and input. Retained conversations hydrate from
their existing owner on return. A successful frame cannot erase a failed control
or input explanation, and uncertain input is never replayed.

HASHI3 adoption and the production Workbench observations on October 8 are
recorded in [the input repair](repairs/DESKTOP_INPUT_REPAIR_20261008.md). Unlocked
physical input and browser-emulated touch are distinct from physical-phone and
complete Windows credential-login acceptance.

## Validation

```bash
python -m pytest -q tests/test_desktop_session.py tests/test_capability_broker.py tests/test_device_control_worker.py
python scripts/check_protected_core_changes.py
```

The new tests exercise actual HTTP envelopes, input ordering/replay, cross-actor
rejection, generation fencing, bounded JPEGs, shared frame work and the existing OS
file lock (using synthetic screen/input providers off Windows). Existing Broker and
Worker tests also run. These checks do not establish physical Windows input, frame
quality or network latency. Local acceptance must check multiple monitors/negative
origins, 125-200% DPI, dragging, Chinese input, native capture errors, disconnection,
15-minute resource stability and mutual exclusion with an actual Agent.

Mutation proof: temporarily bypassing worker-generation comparison made the pinned
worker test fail; restored code passed. No mutation is retained.

## Rollback

Release/close the desktop and disable the Workbench desktop entry from the
Workbench settings to deny access; there is no HASHI global desktop switch
anymore. No desktop polling continues when the frontend leaves.
To roll back code, follow ordinary Functions/Remote/Worker adoption at the same
scope; do not cold-restart protected Core merely as a testing shortcut.


## Native picture admission

The existing authenticated desktop session attempts the native display/frame
operation even when status metadata labels the desktop locked or secure, or its
name differs from Default. A state label alone is not proof of failed capture.
Independent monitor previews follow the same native result instead of adding
a second status-label rejection. The frontend continues to show available main
pictures and previews. Actual native failures remain errors; no blank/fabricated
success frame is substituted. Existing SYSTEM bridge ownership/session checks,
PAO target binding, exclusive control leases and input ordering remain unchanged.

Focused regression covers the real native admission method through the desktop
controller and JPEG encoding, preview labels changing during capture, and actual
native failure propagation. These fixtures do not claim a physical Winlogon
screen was captured or change the user's desktop session.
