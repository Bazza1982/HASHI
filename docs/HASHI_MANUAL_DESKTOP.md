# Manual Desktop v1 — Functions / Windows Worker

Status: implementation candidate on `feature-virtual-desktop`; Windows and external-client live acceptance pending.
Baseline: `3004fb1799928506880b7ae3139edf8b96937625`.

Functional owner: Frontend Connectors for the public desktop ingress/projection;
PAO remains the single authority for capability registration and write leases.
Engineering placement: replaceable Functions plus the existing Windows interactive
Computer Worker. No `CORE_SOURCE_PATHS` file is edited. No model/Agent Run is created.

## Enable locally

1. Pull this branch in a separate checkout and preserve unrelated local changes.
2. In the selected instance's ignored `agents.json`, set `global.desktop_enabled`
   to `true` using the established revision-safe configuration workflow. Default is
   false. `HASHI_DESKTOP_ENABLED=1` is an optional process-local alternative.
3. The existing ignored `secrets.json` MUST contain a nonempty
   `workbench_admin_token`. Reuse the existing value. Do not overwrite the file or
   add another pairing system. The existing authenticated Remote proxy injects
   this token on its loopback Backend API hop.
4. Adopt the updated shared Functions using the instance's established hot adoption
   procedure. These are shared services, not merely one Agent's code. Source pull
   and running-generation adoption are distinct facts.
5. Adopt the updated Remote process separately: its allowlist now forwards
   `X-Desktop-Meta`, and an uncertain desktop write must not retry another loopback
   address. Do not widen the existing Remote lifecycle scope.
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

## Protocol

`POST /api/v1/desktop/operation`, JSON, <=32 KiB. Requires the existing Backend
API admin token on every call. A session ID alone grants no authority. Client ID
is tab correlation, not an authentication credential. Operations:

- `targets`: sanitized Windows computer capabilities with all desktop actions.
- `open`: an exact target including worker generation; server returns session ID
  and display metadata. No new OS login session is created.
- `frame`: session ID and optional `after_frame`; returns `image/jpeg` or HTTP 204
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
8 seconds without renewal. A stale cleanup request from an Agent cannot release
someone else's active manual input.

## Bounds and privacy

One shared in-memory JPEG per worker, <=512 KiB; at most 1600x900 (1280x720 small),
quality starts at 75, four bounded encoding attempts maximum. GDI captures/scales
the selected monitor/region directly; no screenshot temp files. Capture/encoding
uses the existing request worker thread, not Core/HTTP event loops. Concurrent frame
requests do not start independent capture jobs. Active max 2 FPS, stable idle .5 FPS;
no requests means no capturing. Full-resolution 4K frames are never transmitted.

Only pending mouse moves may be coalesced by clients. Input validation checks finite
normalized coordinates, display revision, a recently checked frame, sequence and
lease. Releases remain possible with expired frames. Displays are revalidated at
input time. Unicode text uses SendInput, never clipboard sync. No command/shell/file
transfer/audio interfaces are provided. No raw frame or input content is logged.

The Windows login/lock/UAC secure desktop is unavailable. Ordinary workers cannot
inject into higher-integrity processes. No privilege escalation or UAC bypass is
part of this feature. Other OS adapters are not implemented.

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
origins, 125-200% DPI, dragging, Chinese input, secure-desktop failure, disconnection,
15-minute resource stability and mutual exclusion with an actual Agent.

Mutation proof: temporarily bypassing worker-generation comparison made the pinned
worker test fail; restored code passed. No mutation is retained.

## Rollback

Release/close the desktop, set `global.desktop_enabled=false` and adopt that instance
configuration normally. No desktop polling continues when the frontend leaves.
To roll back code, follow ordinary Functions/Remote/Worker adoption at the same
scope; do not cold-restart protected Core merely as a testing shortcut.
