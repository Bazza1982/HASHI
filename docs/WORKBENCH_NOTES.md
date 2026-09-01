# Workbench Retirement Notice

Architecture: [HASHI Frontend Connector Architecture](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md)

Workbench has retired and is no longer shipped or supported by HASHI.

The retired component's frontend, Node server, build assets, launchers, local
data, dependencies, and UI-specific tests have been removed from this
repository. HASHI startup, onboarding, packaging, and release checks no longer
start or build Workbench.

Any successor graphical frontend is developed in a separate repository. It may
consume HASHI's generic Connector and API contracts, but it is not part of the
HASHI program or this architecture.

## Compatibility Names

Some established internal names remain to avoid breaking HASHI deployments and
API consumers:

- `global.workbench_port` is the HASHI Backend API port.
- `orchestrator/workbench_api.py` implements the Backend API.
- `workbench_admin_token` and `X-Workbench-Token` remain compatibility names
  for existing Backend API authentication.
- `/workbench/v1/*` remains a compatibility route family in Hashi Remote.

These identifiers do not mean that Workbench is still included. New HASHI
documentation and user-facing status text call the service **Backend API**.

## Runtime Semantics

Telegram, TUI, scheduler jobs, HChat, and authenticated Backend API clients use
the same in-process agent runtimes, queues, sessions, memory, and transcripts.
Gateway and Backend API contract tests remain in HASHI because they protect
those public runtime interfaces, not the retired Workbench UI.
