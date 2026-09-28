# HASHI3 Function Adoption and Remote Restart Decision

## Decision

Approved by the user on 2026-09-19:

1. shared and Agent Function changes are adoptable with `/reboot max` or a
   targeted `/reboot`; Remote changes require a separate lifecycle action;
2. `/restart` works through a running authenticated HASHI Remote regardless of
   whether that Remote was started as a supervised process or bundled child;
3. protected Core remains unchanged unless a separately authorized Core major
   migration is required.

## Implementation boundary

PAO owns reboot admission, durable receipts, and evidence reconciliation.
As revised by the user for HASHI1 on 2026-09-27, `min|same` target only the
requesting Agent, while numbered and group scopes target exactly those Workers.
Only `max` invokes the existing protected-Core shared handoff to start a new
shared Functions process and all running Agent Workers. The successor verifies
the Core receipt, changed shared PID, committed generation, and each Worker.
Remote remains a separate live process and is not restarted or used as reboot
success evidence. The successor shared startup skips Remote lifecycle setup
entirely. If Remote code changes, it needs a separate authorized
lifecycle action. The qualified generation still includes the Remote source
closure, but qualification does not mean Remote has adopted that source.
On HASHI1's POSIX runtime, a bundled Remote child starts in its own session,
outside the shared Functions process group. Closing the old shared group during
`max` therefore neither waits on nor terminates that Remote.
An uncommitted qualified source file is reported separately from a candidate
startup failure. The ordinary notice states briefly that the software update
is incomplete; file paths remain diagnostic detail. Reboot admission fences
new messages for its target set, while existing work completes. Shared handoff
cancels only an idle Telegram long poll, without delaying on its timeout.

One compatibility bridge is required when the currently running shared
generation predates whole-Function reboot semantics. Its newly qualified Agent
Workers wait for the legacy `same|max` Worker transaction to finish; the
deterministic target leader then publishes one request through the existing Core
handoff protocol. The successor PAO promotes the legacy receipt only after the
Core replacement receipt proves commit. It uses the first replacement's Worker
PIDs as the second replacement's baselines, then performs the same shared PID,
generation, and Worker checks. A failed candidate never upgrades the
legacy receipt store or prevents the old shared generation from recovering.

The legacy Agent-Worker qualification step deliberately retains the historical
asset closure when its requested module set does not include `remote.main`.
This keeps the candidate digest verifiable by the running legacy generation.
The subsequent Core-owned whole-Function qualification explicitly seeds
`remote.main` and verifies the Remote/restart source closure without restarting
the live Remote. The pre-handoff Agent-only digest therefore differs from the committed
full generation by design; receipt promotion validates each on its own side of
the handoff, then requires every successor Worker to report the committed full
generation. Compatibility therefore enables the handoff without weakening the
final broad-generation asset contract.

Frontend Connector/Remote Functions own restart provider discovery and the
fixed restart launcher. A running Remote with an authenticated `rescue_restart`
capability is valid in child or supervised mode. On Windows the network-facing
Remote remains a Limited scheduled task. A separate exact-instance
`HashiRestart-<instance>` task runs Highest and accepts no action, executable, or
target from the Remote request; it invokes only the fixed instance controller.
The actuator stops the previous Core, triggers the exact-instance
`HashiRuntime-<instance>` task, verifies Backend readiness, and exits. The
runtime task provides the replacement Core's isolated elevated launch boundary;
the restart actuator does not remain attached to that Core. Remote can therefore
remain online and the same fixed restart can be used repeatedly. If Core and
Remote already share privilege and no actuator exists, the fixed controller is
used directly. Process termination errors are
surfaced rather than swallowed. The Windows runtime task launches `main.py`
directly with the instance bridge-home and saved Agent selection. It runs hidden
and writes separate stdout/stderr launch logs; the interactive menu batch file
is not part of Remote recovery. API Gateway startup follows its canonical
persisted instance setting.

## Adoption and verification

The implementation is entirely outside protected Core. Source validation,
focused lifecycle tests, Windows scheduled-task contract tests, the minimum
runtime gate, and full offline tests are required before handoff. Source changes
and offline tests do not prove that the currently running HASHI3 generation has
adopted them. Live `/reboot` and `/restart` remain separately authorized
operational actions.

## Live verification — 2026-09-20

The user explicitly authorized HASHI3 `/reboot` and `/restart` testing. Protected
Core was not changed.

- Two consecutive local `/restart` commands completed successfully through the
  authenticated running Remote. Core changed `9916 -> 29424 -> 17452`; both
  restart task results were zero, and Remote stayed on PID `32748` throughout.
- Broad `/reboot max` receipt `1d45fdea0cbc4e72aafedc9997943c40`
  finished `succeeded` and committed. Core remained PID `17452`, shared
  Functions changed PID `5248 -> 33172`, all eight Worker PIDs changed and
  returned online, and Remote adopted the generation at PID `16868`.
- The reboot correctly retained generation
  `sha256:5f8d035fbb101769a56ecff06be5d36e3f1d82b043ea01cc3ee3e74f37da8add`
  because the qualified source bytes were unchanged. PID replacement, matching
  generation, health, and the committed receipt are the adoption proof; a
  changed generation hash is not required for a same-source reboot.
- The focused lifecycle suite completed with 176 passed and 2 platform skips.
