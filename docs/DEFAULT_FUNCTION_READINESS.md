# Default function readiness

Normal installed capabilities must be usable without an undocumented enablement
step. The deployment owns credentials, fixed lifecycle launchers, runtime
qualification and platform binding. Missing prerequisites must be reported
truthfully. An owner's explicit opt-out is retained.

## Corrected defects

| Area | Defect | Result |
| --- | --- | --- |
| Instance restart | Fixed lifecycle controls inherited the generic terminal L3 restriction. | Authenticated fixed start/restart/reboot are available under the normal terminal policy. Authentication, target identity and peer trust remain enforced. |
| Split installations | Restart selected scripts, runtime qualification and fixed tasks from the private data home. | Program CodeRoot and private home are explicit through Remote, installation, tasks and native controllers. |
| Windows deployment | Split-home fixed tasks could be omitted; setup failures were treated as a warning. | Remote installation prepares the fixed actuator; setup failures are surfaced. Task upgrades retain the principal, logon type and task DACL, and reject a foreign home. |
| POSIX restart | The launcher stopped Core alone after a five-second grace period. Detached shared Functions could survive. | Capture Core descendants before shutdown, allow graceful cleanup, then verify the captured identities are gone. Signals are fenced against PID reuse. Other instances and the exact-home child Remote provider are preserved. |
| Readonly runtime control | Controller, candidate-runtime and OCR probes, plus native child entrypoints, omitted Python -B, permitting bytecode writes into a qualified interpreter base. | Those entrypoints now run readonly. Exact original sealed files must be verified; extraneous caches are preserved before restoration, never adopted into a new seal. |
| Personal credentials | Local API credential bootstrap depended on a pre-existing Remote key. | Personal startup privately prepares separate missing instance Remote/local API keys once, retains existing secrets, and rejects conflicting or malformed publications. Enterprise credentials retain their own owner. |
| API gateway | Fresh configuration, first model-only save and portable launch defaults left the gateway off. | Fresh configuration and deployment are ready by default. Explicit disable and malformed-file failure handling remain effective. |
| Desktop interface | Normal configuration returned feature-disabled before a user could use the authenticated interface. | The interface is available by default, with the existing authentication and session boundaries. Explicit opt-out remains possible. |
| HER habits | A fresh backend silently skipped habit retrieval and learning until an extra switch was changed. | The shipped habit/learning lane defaults on. Explicit owner opt-out and ineligible internal requests still suppress learning. |
| Exchange | A complete configured connection was silently ignored if the redundant enabled field was absent. | Connection configuration implies use; an explicit false still disables it. Empty/unconfigured instances do not publish agents or invent credentials. |

These are Functions, Remote, platform launcher and deployment changes.
Protected Core code, its wire contracts, and the independent Core/Functions
dependency lifecycles are unchanged.

## Audit coverage

The initial tracked inventory contained 2,767 files. Static review parsed 967
Python modules without parse failures and collected 588 false/default-off
candidates plus 162 extended environment/CLI/deployment gate candidates.
These are candidate occurrences, not counts of defects. The 97 canonical
slash-command entries, manager construction, tool exposure, configuration
owners and deployment profiles were reviewed by functional family.

| Functional family | Readiness finding |
| --- | --- |
| Start, stop, restart, hot adoption, Remote rescue | Fixed lifecycle defects above; receipts must verify old/new process identity, health, instance and qualified runtime rather than infer success from accepting a command. |
| Agent/session creation, command dispatch, working modes, queues | Core registries and managers are constructed normally. Stored inactive agents/jobs and transfer-paused copies are state, not deployment feature gates. |
| Telegram, Backend API, gateway, Workbench commands | Telegram requires the user's bot identity; no master opt-in for ordinary replies. Gateway and local API bootstrap defects are corrected above. |
| Tools, MCP, browser, files, device control | Tool exposure follows the admitted registry and platform capability. The adapter's initial false MCP state changes when the real registry binds; it is not a separate user switch. Generic execution/authority boundaries remain enforced. |
| Native audio, live voice, calls, desktop | Normal audio/call contracts remain available subject to model credentials and real hardware/media capability. Desktop default corrected. Camera state and configured video-TTS selection are capabilities, not a global deployment switch. |
| HER, habits, companion, fallback | Shipped habits now default on. Retired HERV2 multi-stage tool routes stay retired. Automatic companion experiments and cross-provider fallback are separate model/cost/automation choices; ordinary main-model operation does not depend on enabling them. |
| Memory, HCC, Wiki, skills | Manual memory/search tools remain registered. Automatic saved-memory injection, optional prompt slots and skill schedules are user preferences. Turning off injection does not remove the manual retrieval capability. |
| Scheduler, tasks, nudges, notifications | New jobs are explicitly scheduled by their owner. Paused/disabled stored jobs are not globally re-enabled. Optional extra notification/presence preferences do not suppress normal final replies. |
| WhatsApp and Exchange | Explicit WhatsApp start persists its connection state automatically. It still needs account linking. Configured Exchange no longer needs the redundant enable flag; credentials, peer registration and publication choices remain necessary. |
| Enterprise/team | Authentication, organization bootstrap and connector identities belong to the selected profile. Single-replica defaults do not require distributed scheduler leases; multi-replica deployment requires the matching database/lease configuration. Ingress, SIEM, HPA and load-rehearsal toggles select infrastructure that may not exist. They are not prerequisites for personal/local functionality. |
| Presentation, provenance, debug | Final style, speech performance, watermark/provenance policy, extra presence and debug reporting are optional output/data preferences. Their absence does not block the underlying command, file, speech or reply operation. |

## Capabilities that must not be advertised as working

- Standby has a control-plane/profile implementation, but live media capture,
  judge, arming and recording still return explicit qualification blockers.
  Changing its enabled field cannot supply the missing implementation.
- The default voice catalogue includes a disabled OpenRouter Gemini TTS entry.
  That particular route is not a validated enabled provider. Other qualified
  TTS providers remain separate and usable; adding a key alone is not evidence
  that this route works.
- Enterprise external connectors, public demo service, optional native models,
  browser/media hardware and paid providers require their real credentials,
  resources and end-to-end verification. Offline contracts do not prove those
  external services are operational.

The audit covers readiness/default gating and the affected regressions.
It is not a claim that every paid provider, hardware device, enterprise
connector or unfinished product feature was exercised live.

## Verification contract

Focused tests reproduce the old default/gating failures, then verify the new
behavior together with explicit opt-outs, authentication failures, revision
conflicts and independent runtime selection. Native PowerShell tests parse and
execute real controllers against isolated scheduler boundaries, including
split roots, wrong-home rejection, task principal and DACL preservation. POSIX
tests stop a real disposable detached Functions process while preserving an
unrelated process.

Live acceptance uses committed qualified code on non-production instances,
the ordinary /restart command, durable terminal receipts, old/new process
creation identities, healthy shared Functions and every expected Worker.
Source synchronization, loaded runtime, external business verification and
production adoption are distinct states. HASHI4 must not be restarted as part
of this repair.
