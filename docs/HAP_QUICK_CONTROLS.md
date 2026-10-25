# HAP Quick Controls

Functional owners: Frontend Connector (authenticated AP view/transport) and PAO (existing Agent settings).
Engineering layer: Functions. Protected Core, configuration locations, shared model catalogue and dependencies are unchanged.

## Contract

The local AP client uses GET/POST /api/agentpresence/controls. Every request requires the existing AP token, exact instance_id, a literal loopback connection and a supported personal profile. The AP token does not authorize the general command API or enterprise settings. The adapter publishes no credentials, file paths or raw configuration.

Read returns the HASHI-owned AP route revision, current Agent, active Agent options, instance registry projection, effective backend_catalogue, actual engine/provider/model/effort and busy state. The Worker supplies current facts over the existing private runtime.slash transport; old Workers return an unavailable state instead of fabricated controls.

Write accepts one explicit setting, expected Agent/route/configuration versions and a request_id. Agent changes use agent_presence_settings.set_default_agent and its revision check. Backend/provider/model/effort use existing commands, permissions, model policy, busy guards, BackendManager and WorkspaceStateStore. Settings remain scoped to the named Agent, while the AP default route is instance scoped. Character packages are independent.

The internal transport prefix is rejected from ordinary chat. HAP controls never submit a model prompt. Duplicate request IDs are coalesced for ten minutes within the shared service process; reuse with a different body is rejected. The desktop client does not retry writes across timeouts or process replacement.

## Confirmed settings

Command execution success is not application proof. The adapter compares actual fields after each step and verifies the relevant saved fields through the existing strict state owner. BackendManager.strict_settings_save propagates disk/conflict failures for these confirmed operations while preserving compatibility for other callers. The view reports uncertainty and reads actual state after a lost response; it does not replay or roll back an unknown write.

HERV3 Provider selection follows Engine selection and re-reads Model/Effort. These steps can partially commit: a later refusal is reported with the actual remaining configuration. No new state store or provider/model/effort list is introduced.

## Agent FYI

- HAP now has a compact gear panel consuming this contract.
- Changing its HASHI Agent changes this instance's future AP ingress route, not its visual character or Agent lifecycle.
- Independent companion visibility keeps PTT, text, audio and the floating communicator alive. Pausing remains separate.
- The communicator stays in the normal desktop's topmost window band without taking focus. Full/Half/Pet and pause do not hide it automatically.
- Existing full context menus and ingress APIs remain available.

## Approval, implementation and adoption

2026-10-25: user approved the investigation proposal and explicitly required the communicator to remain floating above desktop layers.

Implementation: local Functions adapter and strict persistence confirmation; HAP WPF controls and independent presentation visibility. No Core source changes. Focused tests cover authenticated boundaries, route conflicts, idempotency, busy refusal, real command persistence and save failure.

The first deterministic gate returned 865 passed, 1 skipped and 3 source-qualification failures because these exact Functions files were not yet committed. Commit qualification and running adoption are distinct subsequent checks; see the HAP dated verification report for final evidence.
