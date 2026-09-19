# HASHI3 Function Adoption and Remote Restart Decision

## Decision

Approved by the user on 2026-09-19:

1. every non-Core Function change is adoptable with `/reboot`;
2. `/restart` works through a running authenticated HASHI Remote regardless of
   whether that Remote was started as a supervised process or bundled child;
3. protected Core remains unchanged unless a separately authorized Core major
   migration is required.

## Implementation boundary

PAO owns reboot admission, durable receipts, and evidence reconciliation.
Targeted `min`, numbered, and group scopes keep the per-Agent Worker transaction.
Broad `same|max` records one request and invokes the existing protected-Core
shared handoff. The successor shared process verifies the Core receipt, changed
shared PID, committed generation, every running Agent Worker, and enabled Remote
adoption before reporting success. The qualified generation includes the
`remote.main` service closure and the fixed Windows Remote/restart launcher
chain, so an absent, modified, or uncommitted lifecycle component is rejected
before cutover.

One compatibility bridge is required when the currently running shared
generation predates whole-Function reboot semantics. Its newly qualified Agent
Workers wait for the legacy `same|max` Worker transaction to finish; the
deterministic target leader then publishes one request through the existing Core
handoff protocol. The successor PAO promotes the legacy receipt only after the
Core replacement receipt proves commit. It uses the first replacement's Worker
PIDs as the second replacement's baselines, then performs the same shared PID,
generation, Worker, and Remote checks. A failed candidate never upgrades the
legacy receipt store or prevents the old shared generation from recovering.

Frontend Connector/Remote Functions own restart provider discovery and the
fixed restart launcher. A running Remote with an authenticated `rescue_restart`
capability is valid in child or supervised mode. On Windows the network-facing
Remote remains a Limited scheduled task. A separate exact-instance
`HashiRestart-<instance>` task runs Highest and accepts no action, executable, or
target from the Remote request; it invokes only the fixed instance controller.
If Core and Remote already share privilege and no actuator exists, the same
fixed runner is used directly. Process termination errors are surfaced rather
than swallowed.

## Adoption and verification

The implementation is entirely outside protected Core. Source validation,
focused lifecycle tests, Windows scheduled-task contract tests, the minimum
runtime gate, and full offline tests are required before handoff. Source changes
and offline tests do not prove that the currently running HASHI3 generation has
adopted them. Live `/reboot` and `/restart` remain separately authorized
operational actions.
