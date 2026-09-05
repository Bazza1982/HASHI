# HASHI Architecture

The canonical architecture and engineering guideline is
[`docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`](docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
The normative Python/ABI and transactional function-generation decision is
[`docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md`](docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md).
The current HASHI3 implementation and promotion boundary is
[`docs/HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md`](docs/HASHI3_RUNTIME_CLOSEOUT_2026-09-05.md).
The accepted HASHI3 Browser/Computer Worker, cross-WSL/Windows discovery, lease,
and silent-background-runtime target is
[`docs/HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md`](docs/HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

In short:

```text
stable process core
    -> stable per-Agent route handles
        -> isolated, verified Function Worker generations
        -> local platform adoption
            -> local instance configuration
```

Changes should be local, derived from a single fact owner, and replaceable with
`/reboot` when they are functional. Python, dependencies, Core sources and
protocol/API versions require a planned Core migration. In-process module
reload is forbidden. Contributor workflow and required checks are in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

Core-owned Telegram and Workbench ingress sends slash control through the
versioned Function Worker RPC. Each Worker also owns a dedicated out-of-band
provider-interrupt lane, so stop, steer, focus, and retry can terminate active
CLI work before normal event-loop cleanup. Session Workzone slots publish only
their exact enabled roots to backends and tools; implementations must not widen
multiple roots to their common parent. HASHI Remote rescue remains a separately
deployed `L3_RESTART` sidecar, and its local hot-reboot hop must use the
token-protected Workbench admin command endpoint.
