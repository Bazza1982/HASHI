# External Workbench Gateway Notes

HASHI Workbench is an independent application. This repository contains no
Workbench frontend, Node server, build assets, launcher, or private Workbench
state. HASHI provides only the runtime interfaces that an authenticated
Workbench client needs.

## Connection Boundary

- The local Python Backend API is implemented in
  `orchestrator/workbench_api.py` and listens on `global.workbench_port`. The
  configuration and module names are retained for compatibility.
- Hashi Remote exposes `GET /workbench/v1/status` and
  `/workbench/v1/proxy/api/*` as the authenticated Workbench Gateway.
- Remote requests use the existing Hashi Remote shared-token HMAC protocol.
- The gateway terminates remote authentication and forwards the request over a
  loopback-only hop. If a local Backend API admin token is configured, Remote
  injects it as `X-Workbench-Token`; it is never returned to the client.
- A Workbench client may discover several HASHI instances and connect to any
  instance that has a compatible gateway and the same shared token.

## Shared Runtime Semantics

Telegram, TUI, scheduler jobs, HChat, and authenticated external clients feed
the same in-process agent runtimes. Consequently:

- requests use the same per-agent queue and backend session state
- transcript order follows queue order regardless of the originating surface
- `/new`, `/fresh`, `/model`, backend switches, `/retry`, and `/resend` affect
  the same shared runtime
- durable user/assistant deliveries remain in each agent's
  `transcript.jsonl`; external clients do not own a second authoritative
  transcript

The client-neutral Session API persists canonical Session, Message, Run, and
Event state only after its fail-closed qualification gate is enabled.

## Operational Boundary

- HASHI must continue to run if the external Workbench is absent or stopped.
- Workbench design-only features must continue to run without HASHI.
- HASHI launchers do not start, stop, update, or package Workbench.
- Workbench releases, UI tests, Node dependencies, local design data, and
  commercial/private code belong only to the independent Workbench repository.
- Gateway and Backend API contract tests remain in HASHI because they protect
  the public integration boundary.
