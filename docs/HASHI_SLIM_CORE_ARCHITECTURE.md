# HASHI Minimal Core and Function Processes

Status: accepted for HASHI2 on 2026-09-07; source implementation complete.
Live adoption requires the operator's cold restart. Earlier HASHI3 closeout
records describe the previous Core API 2 topology, not this migration.

## Decision and ownership

Core is a product-neutral process supervisor. It owns the interpreter and
compatibility fingerprint, the instance OS lock, immutable artifact verification,
JSON process protocol, process creation, replacement and exit handling. Its sole
source manifest is `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.

Every product capability runs in Functions or configuration. In particular,
models/defaults/effort catalogues, provider and media execution, PCM, HER policy,
commands, UI text and rendering, configuration interpretation, scheduling,
background jobs, routing, Telegram/WhatsApp and Backend API are not Core code.

```text
stable Core: runtime contract + instance lock + process supervision
    |
    +-- verified shared Functions process
            |-- PAO managers, scheduler, background jobs and shared routing
            |-- Frontend Connectors, Backend API, model/media API Gateway
            |-- immutable shared catalogue, UI and HER policy generation
            +-- stable Agent handles -> isolated per-Agent Function Workers
```

`main.py` loads no product module, including through lazy imports. The one
verified child bootstrap is the only dynamic product import boundary. The
Core never imports the product entrypoint into its own module space.
`runtime-entry.json` selects the Function qualification and execution entrypoints.
Qualification runs in an isolated subprocess before any active service is gated.

`orchestrator.runtime_app.UniversalOrchestrator` and
`orchestrator.manager_registry.FUNCTION_MANAGER_SPECS` own the shared Functions
process. They retain object identity during Agent-only replacements. Their
entire functional closure is pinned to an immutable artifact; they are not
cached mutable modules in Core under a different filename.

## Two explicit replacement scopes

### Agent-only replacement

`/reboot min`, a numbered target, `same` and `max` keep their existing target
rules. They qualify real candidate Agent Workers, close only selected route
gates, drain, activate, atomically publish selected pointers and retire old
Workers. Candidate failure resumes the old selected Workers; unselected Agents,
shared services and Core remain online. No mode silently widens into a shared
service replacement.

Agent updates can adopt new models, effort choices, commands and execution code
without replacing the shared Functions process. Shared views (for example the
model API catalogue or terminal display) retain their own installed generation
until an explicitly authorized shared replacement.

### Shared Functions replacement

The local operator entry is `python main.py --replace-functions`, using the
normal `--bridge-home` when needed. It submits a request; acceptance is not a
completion receipt. This is a broad operational action requiring the user's
scope, just like reboot. It never authorizes itself from a code-edit request.

1. Qualify and hash the complete candidate closure and assets out of process.
2. Verify the artifact and prepare a new shared process without binding ports
   or initializing providers.
3. Reject new external work, pause scheduling and finish the accepted Telegram
   batch, preserving its update offset.
4. Drain active API requests, Agent work and background jobs. Busy/failed drain
   rejects replacement and resumes the existing process. Jobs are not killed
   to force an upgrade through.
5. Close the old shared process and its Agent Workers; retain the Core PID and
   instance lock. Start the candidate from its pinned artifact, with exactly the
   previously running Agent set and preserved Telegram offsets.
6. Commit the replacement and reopen intake. A later observability/transport
   error is a post-commit fault, not a claim that the old generation resumed.

Preparation failure leaves the original shared PID unchanged. Startup failure
before commit restores the previous shared artifact and each Agent's own
immutable generation, including Agents independently updated since shared startup.
Durable Session, journal, schedule, configuration and Memory+ stores stay in the
instance home. A failed rollback is reported as failed, never as successful
recovery. This mechanism does not reverse an incompatible database migration.

Shared replacement has a service gap while processes and listeners transfer;
it is **not zero downtime**, and existing streaming/client connections may need
to reconnect. It does not cold-restart Core. Keep normal feature updates scoped
to the relevant Agent whenever the shared service itself has not changed.

`state/instance/kernel.json` records Core PID, shared PID, shared generation and
outcome. `replacement-<request-id>.json` records completion. Backend API health
also reports shared Functions separately from per-Agent generations. Source,
qualified artifacts and running generations are different facts.

## Qualification and recovery

The stable, product-neutral import-purity guard runs before the first product
import in a spawned process. Function manifest/schema and cross-module contract
checks cover both shared and Agent code. Manifest routing
includes package initializers, literal lazy imports and manager registry entries.
Missing project modules fail closed instead of falling back to mutable source.
Core paths cannot be shadowed by artifacts. Runtime, Core digest, source bytes,
asset bytes and executable modes are checked before process use.

Enabled post-Turn observers declared by active Agents are additional qualification
roots. PAO reads factory module names from the instance's observer declarations,
resolving workspaces through the existing path owner without loading PCM or
executing factories. Cold qualification and Agent qualification include their
transitive project dependencies. Memory and disk caches are reusable only when
they cover the current enabled declarations; enabling a previously omitted
observer forces qualification. Disabled declarations add no dependency. The
immutable import guard remains authoritative; a missing or invalid project
module rejects qualification instead of reading mutable checkout code. Existing
running artifacts do not change when a configuration file is edited.

Core and Function API are now 3. CPython remains 3.12.13, Agent worker protocol
remains 1 and the Function artifact schema remains 2. This is an authorized Core
migration, not permission to bypass compatibility checks on old running Core.

Unexpected process recovery uses the last committed shared artifact and exact
per-Agent generation checkpoints, never checkout edits or the original Agent
selection. Telegram offsets are persisted when accepted and recovery never
requests pending-message deletion. Individual connector activation failures
are reported as degraded health and retried without blocking healthy connectors.
The child observes parent IPC closure and stops its Workers. On POSIX,
shared children have dedicated process groups, with bounded cleanup of remaining
children. Windows uses an OS Job Object with kill-on-close descendant ownership,
plus bounded exact-PID tree cleanup; native Windows live
qualification remains a separately reported platform check.

## Startup presentation and Connector health

PAO startup and Frontend Connector status belong to the shared Functions process.
Worker preparation can finish before shared commit opens Telegram intake. During
that interval health remains `connecting`/not ready, and the final startup table
is deferred. A prepared Worker does not prove a connected transport. After
activation, startup presentation and health use the actual ingress and Worker
connection state. Failed connectors retain degraded health and automatic retry;
successful retries and later polling recovery clear only Connector-owned issues.
Unrelated service and Agent failures remain visible. Recovery is logged and the
startup table is refreshed when the derived state changes.

Agent Worker logging has no independent console handler. Each Worker retains
diagnostics in `logs/function-workers/worker-<pid>.log` and relays warning/error
records through Function IPC to the shared terminal owner. That owner applies
the existing terminal level and animation filters; Python warnings use the same
route. Credential-shaped text is redacted before either sink, including tokens
embedded in request URLs, and Worker diagnostic files are owner-readable only.
Muting the animation does not discard file diagnostics. This introduces no Core
log policy or new protocol version.

Local desktop wrappers should select the instance/distro and delegate to the
existing `bin/bridge-u.sh` menu. The menu obtains Agent choices and service ports
from the existing launcher configuration view. Wrappers must not copy those
lists or advertise a retired Workbench frontend. Machine paths and identities
remain local configuration; back up a local wrapper before replacement.

The 2026-09-07 startup repairs are PAO/Frontend Functions and launcher changes.
Their source and offline checks do not constitute production adoption. Applying
the shared startup/logging owner requires separately authorized shared adoption;
an Agent-only reboot does not update the shared owner. Live observer execution,
Telegram reconnect and desktop startup remain operator acceptance checks when
restarts or test messages are forbidden by the task.

## Rules that prevent product code returning to Core

- Ordinary feature changes belong in an existing Function owner or configuration.
- The Core import closure includes lazy imports and implicit package initializers.
  Do not hide product imports behind helpers, `importlib`, `exec` or aliases.
- The protected manifest, local hook and existing architecture CI remain the
  safeguards. Product ownership and real process handoff tests run in the normal
  checks; no new approval layer or duplicated ownership manifest is introduced.
- Never unprotect a file while a live Core consumer still imports or owns it.
- No in-process reload, shared Python objects across IPC, target widening or
  cold-restart fallback for a normal Function update.
- New persistence/protocol incompatibilities require a planned migration.

Approval, source/offline evidence and live adoption are recorded separately in
`HASHI2_MINIMAL_CORE_2026-09-07.md`.
