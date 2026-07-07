# HASHI Background Jobs Design

Status: design plus Phase 1/Workbench implementation notes
Date: 2026-07-06
Scope: HASHI runtime, HASHI Remote, Workbench, governed tool execution

## Summary

HASHI should add a first-class Background Jobs subsystem for session-aware
long-running operating-system tasks. This is different from cron, heartbeat,
superloop, delegated agent work, and the existing background LLM generation
path.

The target capability is:

- an agent can start a long-running local or remote process without blocking
  the current conversation;
- HASHI records a durable job id, command metadata, working directory, owner,
  launch source, stdout/stderr logs, and notification preferences;
- HASHI monitors process state and records terminal outcome;
- completion, failure, timeout, or cancellation can be reported back to the
  originating chat, Workbench session, or remote peer;
- operators can list, inspect, tail, cancel, and audit jobs after the original
  turn has ended;
- the design remains compatible with personal/local HASHI and enterprise AAI
  governance.

The long-term design should be reliability-first. It should not be implemented
as a thin wrapper around `subprocess.Popen` hidden inside one command handler.
It should be a managed runtime service with durable state, explicit lifecycle
transitions, bounded logs, security policy, and clean API surfaces.

The design must also follow HASHI's layered-runtime principle:

- keep the immutable core minimal;
- land feature behavior in Layer 2 function services;
- let the kernel hold only a service handle, not job business logic;
- keep local job state in Layer 4 state paths;
- make `/reboot` reload function-layer code and re-run manager recovery without
  requiring a full process restart.

This means "kernel singleton" in this document means a single function-layer
service instance owned through a kernel handle. It does not mean moving process
supervision logic into protected core files.

## Current Implementation Status

As of 2026-07-07, the local managed-background-job path is live for personal
HASHI operation:

- `BackgroundJobManager` can start managed local OS/process jobs and return a
  durable `job_id` immediately.
- Job state includes command metadata, cwd, owner agent, origin metadata,
  process pid/pgid, stdout/stderr paths, terminal return code, bounded last
  output, notification flags, and terminal event metadata.
- Workbench exposes structured job APIs for start, list/detail, tail, and
  cancellation:

```text
POST /api/background-jobs
GET  /api/background-jobs
GET  /api/background-jobs/{job_id}
GET  /api/background-jobs/{job_id}/tail
POST /api/background-jobs/{job_id}/cancel
```

- Workbench job starts accept both shell strings and argv arrays.
- `/reboot` hot reload recreates the Workbench API service so changed route
  handlers are loaded without requiring a full process restart.
- Terminal success/failure notifications can be delivered back to the user.
- Terminal success/failure can also enqueue a one-shot
  `background-job-event` to the responsible agent. The event includes the
  terminal status, job id, command, cwd, log paths, return code, error, and a
  last-output excerpt so the agent can summarize or take the next responsible
  action without blocking the original chat turn.

Validated smoke coverage includes:

- short argv job start through Workbench API;
- subprocess completion and notification delivery;
- command-array handling through Workbench API;
- one-shot completion event enqueue;
- user-visible agent report delivery from a completion event;
- a live 3-minute sleep job that completed, woke Zelda through
  `background-job-event`, and produced a user-facing summary.

Current boundary: the manager owns terminal completion/failure notification and
agent wake-up. Periodic progress notifications during a still-running job are
not yet a built-in manager heartbeat; a job that needs live progress must emit
progress itself or call an approved notification surface intentionally.

## Review Acceptance

This revision critically accepts Akane's review of the design and sharpens the
implementation order.

Accepted:

- implement Background Jobs as a function-layer service owned through
  `ServiceManager`, not as an optional cron/watchdog pattern that agents must
  remember to create;
- use SQLite as the Phase 1 authoritative store. JSON/JSONL may remain useful
  for trigger audit logs and export, but not for live lifecycle state;
- keep OS/process jobs separate from LLM `background_mode`. Do not overload
  `FlexibleAgentRuntime._background_tasks`;
- make registration a side effect of launch: `persist -> spawn -> monitor`
  must happen inside `BackgroundJobManager.start(...)`;
- defer model-facing tools until the manager, store, monitor, and `/reboot`
  recovery semantics are stable;
- preserve synchronous `/terminal/exec`; introduce a separate
  `background_jobs_v1` capability for Remote.

Accepted with staging:

- Long-running OS work should eventually have one legitimate launch path:
  `BackgroundJobManager`. However, Phase 1 should not break existing shell,
  scheduler, or Nagare flows before the manager has production smoke coverage.
  Phase 1.5 should add guardrails for common bypasses such as `nohup`, trailing
  `&`, detached shell patterns, or explicit zero-wait process launch requests.

Rejected for now:

- automatic process adoption after full HASHI restart. Phase 1 will mark
  unsafe running jobs as `abandoned_after_restart` unless identity can be
  verified by a platform-specific implementation and tests.
- exposing model-facing `background_job_*` tools before operator/API surfaces
  prove that the manager can safely start, observe, tail, cancel, and recover
  jobs.

## Existing Code Review

This design is based on a review of the current HASHI code paths that overlap
with background execution.

### Runtime Queue And LLM Background Mode

Relevant files:

- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/runtime_pipeline.py`
- `orchestrator/runtime_lifecycle.py`
- `legacy/bridge_agent_runtime.py`
- `tests/test_runtime_pipeline.py`
- `tests/test_wrapper_commands.py`

Current strengths:

- `FlexibleAgentRuntime` already owns per-agent session directories, media
  directories, Telegram delivery helpers, request ids, transcripts, and runtime
  state.
- `runtime_pipeline.run_backend_generation()` already supports
  `background_mode`: after a configured detach timeout, the LLM generation task
  continues in the background.
- `FlexibleAgentRuntime._register_background_task()` tracks detached generation
  tasks and wires completion callbacks.
- `FlexibleAgentRuntime._on_background_complete()` sends completion/error
  notifications, records transcript entries, notifies request listeners, and
  handles wrapper output.
- `register_request_listener()` and `_notify_request_listeners()` provide a
  useful completion callback pattern. Late listeners are handled through
  `_pending_request_results`.
- The effective LLM detach call chain is:
  `runtime_lifecycle` -> `run_backend_generation()` ->
  `_register_background_task()` -> `_on_background_complete()`.

Current gaps:

- This background path is only for backend LLM generation tasks, not arbitrary
  OS processes.
- It is in-memory. A HASHI restart cannot rebind to a detached LLM task.
- There is no durable job id, no command metadata, no stdout/stderr log model,
  no status query API, and no cancellation surface.
- `_background_tasks` is an `asyncio.Task` set, not a process registry.
- The existing notification path can be reused, but it is not sufficient as the
  persistence or supervision layer.
- `legacy/bridge_agent_runtime.py` still has a parallel background-task shape.
  The canonical implementation for new Background Jobs is
  `FlexibleAgentRuntime`; legacy bridge behavior is out of scope unless a later
  compatibility pass explicitly adopts the new manager.

Design implication:

Background Jobs should reuse the delivery ideas from background LLM mode, but
must not overload `_background_tasks`. It needs a separate process-oriented
manager and persistent state store.

### Scheduler, Cron, Heartbeat, And Nudge

Relevant files:

- `orchestrator/scheduler.py`
- `orchestrator/service_manager.py`
- `tasks.json`
- `managed_active_heartbeats.json`
- `docs/JOBS_CALLBACK_TOKENIZATION_FIX_PLAN.md`

Current strengths:

- `TaskScheduler` is already a long-running service started by
  `ServiceManager.start_scheduler()`.
- It supports heartbeats, crons, nudges, scheduler state, loop safety, missed
  cron detection, and enterprise scheduler leases.
- It can enqueue agent prompts and invoke scheduler skills.
- It treats runtime busy state carefully for nudges.

Current gaps:

- Scheduler jobs are trigger definitions, not process instances.
- It is optimized for "run this prompt/action at a time or interval", not
  "track this OS process until completion".
- Scheduler action execution has short operational timeouts for the scheduling
  action itself. It should not directly own a long-running child process.
- Scheduler skill actions may run much longer than ordinary scheduler actions.
  A scheduler skill must not spawn an unregistered long-running OS subprocess;
  long OS work must be registered through `BackgroundJobManager.start()` and the
  scheduler action should return after job creation.

Design implication:

Do not fold Background Jobs into `TaskScheduler`. Scheduler may trigger a
background job, but the resulting process instance must be owned by
`BackgroundJobManager`. Cron/heartbeat/nudge definitions and process execution
records are separate concepts.

### HASHI Remote Terminal Execution

Relevant files:

- `remote/terminal/executor.py`
- `remote/api/server.py`
- `remote/protocol_manager.py`
- `remote/config.yaml`
- `remote/audit/logger.py`

Current strengths:

- `TerminalExecutor` already classifies commands by `AuthLevel`.
- `/terminal/exec` exists in `remote/api/server.py`.
- Remote capabilities are advertised through protocol status and handshake.
- Remote protocol state already persists message inflight/outbound correlation
  files under `~/.hashi-remote`.
- On restart, `ProtocolManager._mark_nonterminal_inflight_abandoned_after_restart()`
  explicitly marks unresolved protocol messages as abandoned.
- Remote audit already records terminal exec attempts.

Current gaps:

- `TerminalExecutor.execute()` is synchronous from the API perspective: it
  waits for `proc.communicate()`.
- It has a fixed timeout model suitable for short commands, not long tasks.
- `/terminal/exec` returns command output immediately and has no background
  process id.
- There is no `/terminal/jobs` or `/background/jobs` endpoint family.
- Remote capabilities do not currently advertise background job support.

Design implication:

Remote should gain a separate capability such as `background_jobs_v1`, not
mutate `/terminal/exec` semantics in a backward-incompatible way.
`/terminal/exec?background=true` is explicitly rejected for this design because
it would blur synchronous command semantics, create client compatibility risk,
and bypass the stronger job policy surface. Background jobs require a dedicated
capability and endpoint family.

### Workbench And Notification Surfaces

Relevant files:

- `orchestrator/workbench_api.py`
- `tests/test_workbench_admin_notify.py`
- `orchestrator/flexible_agent_runtime.py`

Current strengths:

- Workbench already exposes `/api/admin/notify`, authenticated with the
  Workbench admin token.
- `handle_admin_notify()` resolves a runtime by agent name and sends text to
  the runtime's primary chat when `chat_id` is absent.
- Workbench transfer paths already send operational chat notifications.

Current gaps:

- Workbench has no job API: no job list, job detail, log tail, cancel, or
  notification preference endpoints.
- Notification is text-only. It does not carry structured job metadata for UI
  rendering.

Design implication:

Workbench should expose structured Background Jobs endpoints rather than only
consume completion notifications. Text notification is a delivery channel, not
the source of truth.

### Tool Execution

Relevant files:

- `tools/registry.py`
- `tools/builtins.py`
- `tools/schemas.py`
- `orchestrator/enterprise/execution.py`

Current strengths:

- `ToolRegistry` already controls allowed tools and enterprise governance
  gates.
- Existing enterprise gates already use context fields such as
  `enterprise_workspace_root`, project workspace roots, and shell/network/browser
  enablement checks. Background Job policy should reuse these fields instead of
  inventing a parallel enterprise switch model.
- `execute_bash()` runs shell commands under the agent workspace and enforces
  a timeout.
- `execute_process_list()` and `execute_process_kill()` exist for process
  inspection and signaling.
- Enterprise execution scope can deny file paths outside a project workspace.

Current gaps:

- `bash` is a request/response tool, not a durable job launcher.
- Existing process tools act on system processes, not HASHI-owned job records.
- There is no job-scoped permission model, log redaction policy, or job owner
  model.
- Remote audit currently records terminal exec attempts. Background Jobs need
  symmetric audit events for start, cancel, completion, timeout, and policy
  denial.
- There is no guardrail that prevents agents or scheduler skills from launching
  long-running detached shell work through ad hoc patterns such as `nohup`, `&`,
  or direct `subprocess.Popen`.

Design implication:

Add explicit tool functions only after the manager exists:

- `background_job_start`
- `background_job_status`
- `background_job_tail`
- `background_job_cancel`
- `background_job_list`

Do not add `background=true` to generic `bash` first; that would blur security,
audit, and ownership boundaries.

After the manager is stable, `bash` and scheduler skill paths should detect or
reject common detached-process bypasses and point callers to Background Jobs.
This guardrail is a second step, not the initial implementation surface.

## Design Goals

### Reliability Goals

Background Jobs must:

1. Survive HASHI restarts with clear recovery semantics.
2. Never leave an ambiguous "maybe running" state after manager restart.
3. Persist enough metadata to support audit and operator recovery.
4. Bound stdout/stderr storage.
5. Support explicit cancellation and terminal state recording.
6. Avoid blocking the agent runtime queue.
7. Avoid blocking the Remote API event loop.
8. Provide clear user-visible status and machine-readable state.

### Product Goals

Background Jobs should support:

- long media generation tasks;
- batch document processing;
- remote Windows/WSL jobs;
- local development commands;
- scheduled launch through cron/heartbeat without making scheduler own process
  supervision;
- hchat-originated jobs whose completion can be routed back to the originating
  agent conversation;
- Workbench dashboards and future enterprise audit views.

### Non-Goals

The initial Background Jobs subsystem should not:

- replace cron, heartbeat, nudge, or superloop;
- become a general-purpose distributed queue;
- promise process migration across machines;
- silently reattach to orphaned child processes without proof;
- run arbitrary ungoverned commands in enterprise profile;
- expose raw unbounded logs through Telegram;
- depend on one specific transport such as Telegram.

## Conceptual Model

### Terminology

```text
Task definition
  A scheduled or requested intention, such as a cron entry or user command.

Background job
  A concrete execution instance created from a command, cwd, environment,
  launch actor, and notification policy.

Process
  The operating-system child process started by the job runner.

Notification
  A delivery event emitted when the job reaches a configured lifecycle point.
```

### Core Principle

The process is not the state.

The process is transient. The job record is authoritative. If HASHI restarts,
the manager must recover job records first, then determine whether process
adoption is possible. If adoption is not provably safe, the job must be marked
with an explicit terminal or degraded state.

## Proposed Architecture

### Components

```text
BackgroundJobManager
├── BackgroundJobStore
├── BackgroundProcessRunner
├── BackgroundJobMonitor
├── BackgroundJobNotifier
└── BackgroundJobPolicy
```

Phase 1 should implement these as a compact function-layer module rather than
as a framework. A practical first cut can be one module with four concrete
classes:

- `BackgroundJobManager`
- `BackgroundJobStore`
- `BackgroundProcessRunner`
- `BackgroundJobMonitor`

Policy can start as a focused helper owned by the manager. Workbench and Remote
route handlers should call the manager directly; they should not introduce a
pass-through `ApiAdapter` layer until there is real cross-transport complexity
that justifies it.

### Component Responsibilities

`BackgroundJobManager`

- public service API used by runtime commands, Workbench, Remote, scheduler, and
  tools;
- coordinates validation, persistence, process start, status transitions,
  monitor registration, and notifications.
- runs as a single function-layer service instance per HASHI process, exposed as
  `kernel.background_job_manager`;
- owns per-agent partitions rather than creating separate process supervisors
  per runtime.

`BackgroundJobStore`

- durable SQLite-backed store for Phase 1 local/personal usage;
- records job metadata, lifecycle state, timestamps, return code, pid, paths,
  owner, origin, and notification policy;
- serializes writes through one manager-owned writer path;
- supports startup recovery.

`BackgroundProcessRunner`

- starts OS processes;
- creates stdout/stderr log files;
- uses process groups where supported;
- handles environment filtering;
- avoids shell when argv mode is available;
- supports shell mode only when policy allows it.
- performs process start and wait operations without blocking the runtime event
  loop; blocking platform calls must run through executor-safe helpers.
- enforces stdout/stderr byte caps while writing, not only when tailing logs.

`BackgroundJobMonitor`

- monitors running jobs;
- detects process exit;
- records return code and duration;
- enforces timeout and idle-timeout rules;
- marks jobs abandoned on restart when rebind is unsafe;
- handles log rollover and last-output snapshots.
- records orphan information when cancellation cannot terminate the process
  group.

`BackgroundJobNotifier`

- emits structured completion events;
- sends Telegram/chat text through runtime delivery helpers;
- emits Workbench events;
- can enqueue one-shot `background-job-event` requests to the responsible
  agent for terminal success/failure follow-up;
- optionally sends hchat/protocol replies when a job was remote-originated.

`BackgroundJobPolicy`

- validates command, cwd, environment, actor, auth level, source, and profile;
- enforces personal vs enterprise behavior;
- owns concurrency limits and dangerous-command blocks.
- reuses `ToolRegistry`/enterprise execution context where possible, including
  workspace scope and shell enablement checks.

### Service Ownership

`BackgroundJobManager` should be a kernel-owned function-layer service:

```text
kernel.background_job_manager
service_manager.start_background_jobs()
service_manager.stop_background_jobs()
service_manager.restart_background_jobs()
```

The kernel handle exists so Workbench, runtimes, scheduler, Remote hooks, and
commands can address one manager consistently. The implementation remains in
Layer 2 and must avoid protected core business logic.

This is important for:

- cross-agent job listing;
- admin/operator cancellation;
- per-agent and per-instance concurrency quotas;
- `/reboot` behavior;
- Workbench read-only dashboards;
- future enterprise audit and project scoping.

### Layer Placement

Following `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`:

- `orchestrator/background_jobs.py`: Layer 2 function service.
- `orchestrator/commands/background_jobs.py`: Layer 2 command surface.
- `orchestrator/workbench_api.py` additions: Layer 2 API surface.
- `remote/background_jobs_client.py` or `remote/api/server.py` endpoint wiring:
  Remote function layer.
- local job state under workspace or `~/.hashi/background-jobs`: Layer 4 state.
- enterprise governance integration: enterprise function layer.

Protected core should stay minimal. The only acceptable core-facing change is a
service handle and lifecycle wiring through existing service-manager/reboot
patterns. Process supervision, policy, storage, notification, and API behavior
belong in function-layer modules.

## State Model

### Job Id

Use stable, non-semantic ids:

```text
job_YYYYMMDD_HHMMSS_<8hex>
```

Do not embed agent name, command, or user text in the id. Human labels belong in
metadata.

### State Values

```text
created
starting
running
succeeded
failed
cancel_requested
cancelled
timeout
abandoned_after_restart
adoption_failed
policy_denied
start_failed
```

Terminal states:

```text
succeeded
failed
cancelled
timeout
abandoned_after_restart
adoption_failed
policy_denied
start_failed
```

Notification failure is recorded under `notification.delivery_errors[]` and
does not change the primary process terminal state.

### Metadata Schema

```json
{
  "job_id": "job_20260620_173100_a1b2c3d4",
  "schema_version": 1,
  "state": "running",
  "agent": "zelda",
  "instance_id": "HASHI1",
  "profile": "personal",
  "origin": {
    "source": "telegram",
    "chat_id": 7430217666,
    "request_id": "req-0065",
    "conversation_id": null,
    "from_agent": null,
    "from_instance": null
  },
  "command": {
    "mode": "shell",
    "display": "python batch.py",
    "argv": null,
    "cwd": "/home/lily/projects/example",
    "env_keys": ["PYTHONPATH"]
  },
  "policy": {
    "auth_level": "L2_WRITE",
    "workspace_root": "/home/lily/projects/example",
    "max_runtime_seconds": 14400,
    "idle_timeout_seconds": 1800,
    "max_stdout_bytes": 5242880,
    "max_stderr_bytes": 5242880
  },
  "process": {
    "pid": 12345,
    "pgid": 12345,
    "started_at": "2026-06-20T07:31:00Z",
    "ended_at": null,
    "returncode": null
  },
  "logs": {
    "stdout_path": "background_jobs/job_.../stdout.log",
    "stderr_path": "background_jobs/job_.../stderr.log",
    "last_output_excerpt": "",
    "stdout_truncated_bytes": 0,
    "stderr_truncated_bytes": 0
  },
  "notification": {
    "notify_on_start": false,
    "notify_on_complete": true,
    "notify_on_failure": true,
    "delivered": false,
    "delivery_errors": []
  },
  "created_at": "2026-06-20T07:31:00Z",
  "updated_at": "2026-06-20T07:31:00Z"
}
```

### Storage Location

Personal/local default:

```text
~/.hashi/background_jobs/
  jobs.db
  logs/
    <agent>/
      job_<id>/
        stdout.log
        stderr.log
```

The Phase 1 store should be SQLite, not JSON. SQLite is still local,
dependency-light, and compatible with personal HASHI, but it gives better
concurrency safety, idempotency, Workbench query support, and future migration
paths than an `index.json` file.

JSON may be added later as an export/backup format. If a future local profile
uses JSON for portability, it must use a single writer, per-job metadata files,
atomic rename, fsync where practical, and tests for partial-write recovery.

Remote sidecar default:

```text
~/.hashi-remote/background_jobs/<instance_id>/
  jobs.db
  pending_notifications.db
  logs/
```

Enterprise future:

- store metadata in the enterprise database;
- keep logs in configured object/file storage;
- record immutable audit events through enterprise audit writers.

## Lifecycle

### Start Flow

1. Caller submits `command`, `cwd`, `agent`, source metadata, and notification
   options.
2. Manager validates request through `BackgroundJobPolicy`.
3. Store writes `created`.
4. Store writes `starting`.
5. Runner starts child process with stdout/stderr files open.
6. Store writes `running` with pid/pgid.
7. Monitor registers the job.
8. Optional start notification is sent.
9. Caller receives a durable `job_id`.

### Completion Flow

1. Monitor detects process exit.
2. Monitor captures return code, end timestamp, duration, and bounded tail
   excerpts.
3. Store writes `succeeded` or `failed`.
4. Notifier sends completion/failure notification if configured.
5. Store records notification delivery result.
6. Transcript/audit entries are appended where appropriate.

### Cancellation Flow

1. Operator requests cancellation.
2. Store writes `cancel_requested`.
3. Runner sends SIGTERM or platform equivalent to process group.
4. If still alive after grace period, runner sends SIGKILL when policy allows.
5. Store writes `cancelled` only after termination is confirmed.
6. If termination fails or a child process appears orphaned, store writes
   `failed` with `orphan_pid` / `orphan_pgid` metadata instead of silently
   claiming cancellation succeeded.
7. Notifier sends cancellation summary.

### Restart Recovery Flow

On manager startup:

1. Load all non-terminal jobs.
2. For each job with pid/pgid:
   - check whether pid exists;
   - verify it appears to be the same command and start time when the platform
     provides enough evidence;
   - if verified, mark `running` and resume monitoring;
   - if not verified, mark `abandoned_after_restart` or `adoption_failed`.
3. Never assume a pid is safe to adopt just because the number exists.
4. Emit a recovery audit event.

The conservative Phase 1 rule may be:

- jobs started by the same still-running HASHI process can be monitored;
- jobs found after manager restart are marked `abandoned_after_restart` unless
  a platform-specific process adoption implementation proves identity.

This is less magical than Hermes-style in-memory monitoring, but it is safer and
more trustworthy.

### Reboot Recovery Flow

HASHI `/reboot` is not the same event as a full process restart.

For `/reboot`:

1. `ServiceManager.restart_background_jobs()` stops the monitor loop without
   killing child processes.
2. The function-layer module is reloaded through the existing hot-reload path.
3. A new `BackgroundJobManager` instance opens the same SQLite store.
4. Recovery runs immediately.
5. Jobs from the same still-running HASHI process may be re-associated only if
   the manager can verify identity safely.
6. Jobs that cannot be verified are marked `abandoned_after_restart` with a
   user-visible explanation, pid, and log path.

For full process restart:

- Phase 1 should conservatively mark previously running jobs as
  `abandoned_after_restart` unless platform-specific process adoption has been
  implemented and tested.

User-facing text must be honest:

```text
HASHI restarted while this background job was running. The OS process may still
exist, but HASHI supervision is no longer attached. Check pid/log path before
manual cleanup.
```

## Public Surfaces

### Telegram Commands

Suggested commands:

```text
/bg run <command>
/bg list [running|recent|failed|all]
/bg status <job_id>
/bg tail <job_id> [stdout|stderr]
/bg cancel <job_id>
/bg notify <job_id> on|off
```

The command module should live under `orchestrator/commands/background_jobs.py`
and register through the existing `RuntimeCommand` mechanism.

Telegram output must be concise. Full logs should not be dumped into chat.
`tail` should default to a small bounded excerpt.

### Workbench API

Implemented endpoints:

```text
POST /api/background-jobs
GET  /api/background-jobs?agent=zelda&state=running
GET  /api/background-jobs/{job_id}
GET  /api/background-jobs/{job_id}/tail
POST /api/background-jobs/{job_id}/cancel
```

Planned endpoint:

```text
POST /api/background-jobs/{job_id}/notify
```

Workbench should treat the job store as the source of truth and should render
structured job metadata, not parse chat notifications.

Read-only Workbench endpoints are now available:

```text
GET /api/background-jobs
GET /api/background-jobs/{job_id}
GET /api/background-jobs/{job_id}/tail
```

These endpoints give integration tests, cross-agent visibility, and future
enterprise controls a structured surface that Telegram commands cannot provide.

### Remote API

Add capability:

```text
background_jobs_v1
```

Suggested endpoints:

```text
POST /background/jobs
GET  /background/jobs
GET  /background/jobs/{job_id}
GET  /background/jobs/{job_id}/tail
POST /background/jobs/{job_id}/cancel
```

Remote payload should include:

```json
{
  "command": "python batch.py",
  "cwd": "/workspace/project",
  "agent": "zelda",
  "notify_on_complete": true,
  "origin": {
    "from_instance": "HASHI1",
    "from_agent": "zelda",
    "conversation_id": "conv_..."
  }
}
```

Remote must not silently map background jobs onto `/terminal/exec`. The old
endpoint remains for short commands.

Remote execution policy is owned by the target instance. If HASHI1 asks HERMES
to run a job, HERMES must evaluate the command using HERMES' workspace roots,
AuthLevel limits, environment policy, and enterprise profile. The initiating
instance may request intent; it does not lend its local permissions to the
target.

Remote notification retry must be durable. A Remote sidecar that completes a
job while the local HASHI core or Workbench is offline should persist a pending
notification record and retry later with idempotency keys.

### Tool Surface

Only after the manager exists, add tool schemas:

```text
background_job_start
background_job_status
background_job_tail
background_job_cancel
background_job_list
```

Model-facing tools should return compact structured summaries. Raw logs should
be bounded and redacted.

### Scheduler Internal API

Scheduler integration should use explicit manager methods rather than spawning
subprocesses inside scheduler or skill code:

```text
BackgroundJobManager.start_from_scheduler(...)
BackgroundJobManager.start_from_runtime_command(...)
BackgroundJobManager.start_from_workbench(...)
BackgroundJobManager.start_from_remote(...)
BackgroundJobManager.start_from_tool(...)
```

The source-specific methods may share one internal validator, but separate
entrypoints make audit, policy, and attribution clear.

## Security And Governance

### Command Policy

Background jobs are more dangerous than short shell calls because they persist
after the turn ends. Policy must be stricter than normal `bash`.

Personal profile:

- default allowed under configured workspace;
- shell mode allowed if the agent already has shell/builtin permission;
- dangerous patterns blocked by existing and new policy rules;
- max concurrent jobs per agent defaults to 2, but must be configurable in
  Layer 4 instance configuration;
- remote-originated jobs count against a separate remote quota as well as the
  owning agent quota.

Enterprise profile:

- disabled by default until explicitly enabled;
- requires project workspace scope;
- command cwd must pass `ExecutionScope`;
- must reuse existing enterprise execution context fields such as
  `enterprise_workspace_root`, project workspace roots, shell enablement, and
  audit context rather than creating a parallel policy stack;
- environment allowlist required; default should be empty or minimal;
- audit event required for start, cancel, completion, and failure;
- L3/L4 actions require explicit approval or remain blocked.

### Environment Policy

API, Remote, and model-facing tool calls should default to argv mode and a
minimal environment. Shell mode must be explicit and require stronger policy
approval.

Personal profile may inherit a small safe environment, but should strip obvious
secret patterns by default:

```text
*_TOKEN
*_KEY
*_SECRET
OPENAI_*
ANTHROPIC_*
GITHUB_TOKEN
HASHI_REMOTE_TOKEN
```

Enterprise profile should use an allowlist. Credentials should be injected
through governed connector/secret mechanisms rather than ambient process
environment inheritance.

### Process Groups

On POSIX:

- start with a new process group/session when possible;
- cancel the process group, not just the parent pid;
- record pgid.

On Windows:

- use Windows-specific job object or process-tree termination when available;
- otherwise mark cancellation semantics as best-effort;
- Remote Phase 3 must include a Windows smoke gate before advertising
  production-quality cancellation semantics.

### Log Policy

Logs must be bounded:

- max bytes per stream;
- write-side enforcement of stdout/stderr caps;
- tail excerpts in notifications;
- no unlimited Telegram output;
- optional redaction hooks for secrets;
- log paths scoped under job-owned directories;
- retention policy with `retention_days`, `max_jobs_per_agent`, and optional
  `max_log_bytes_per_agent`.

### Ownership

A job has:

- owning agent;
- owning instance;
- origin source;
- chat id or Workbench session when applicable;
- optional remote correlation.

Cancellation should require the same actor class or an operator/admin channel.

Cross-agent cancellation policy:

- owning agent may cancel its own jobs;
- authorized local operator may cancel any local personal-profile job;
- enterprise project admins may cancel jobs in their project scope;
- non-owner agents may only cancel when policy explicitly grants delegation.

## Notification Design

Notifications are events derived from job state, not the job state itself.

Supported notification targets:

- Telegram chat through `FlexibleAgentRuntime._send_text`;
- Workbench event stream or API notification;
- HChat/protocol reply for remote-originated jobs;
- future enterprise webhook/audit sinks.

Completion summary should include:

```text
Background job completed
ID: job_...
State: succeeded
Exit code: 0
Duration: 12m 41s
Command: python batch.py
CWD: /workspace/project

Last output:
...
```

Failure summary should include stderr tail and an explicit log path hint.

If notification fails, the job should remain in its process terminal state while
recording notification error metadata.

## HChat And Remote Correlation

HChat-originated background jobs need correlation distinct from normal
request/reply.

Recommended model:

- Background job start ack returns immediately to the requesting side with
  `job_id`.
- The job record stores `conversation_id`, `from_instance`, `from_agent`, and
  `reply_target`.
- On completion, notifier sends an `agent_reply` or future
  `background_job_event` message.
- Duplicate completion delivery must be idempotent.

Protocol extension options:

1. Use `agent_reply_v1` with a structured completion text body.
2. Add `background_job_event_v1` later for richer UI/status sync.

Phase 1 can use option 1. Long term, option 2 is cleaner.

## Failure Modes

### Manager Restart

Risk:

- child processes may continue but in-memory monitor is gone.

Policy:

- persist records before process launch;
- on restart, mark non-adopted running jobs explicitly;
- do not pretend completion notification can be guaranteed for jobs that
  outlived the manager unless adoption is implemented and verified.

### HASHI Runtime Offline But Remote Sidecar Online

Risk:

- remote job completes but local agent notification path is unavailable.

Policy:

- Remote stores terminal state locally;
- retry notification when Workbench becomes reachable;
- persist retry intent in `pending_notifications.db` or an equivalent durable
  queue, not only in memory;
- expose status via Remote API.

### Log Explosion

Risk:

- batch jobs can write unlimited output.

Policy:

- use bounded log files or rotation;
- enforce the cap while writing logs, not only when reading tails;
- track truncation in metadata;
- notification includes only bounded tail.

### Duplicate Notifications

Risk:

- restart/retry can send completion twice.

Policy:

- store notification delivery idempotency keys;
- completion delivery should check `notification.delivered` and delivery target.
- Remote-originated jobs should reuse protocol outbound correlation concepts
  where possible so completion events do not become a second, incompatible
  reply-tracking system.

### PID Reuse

Risk:

- reusing a stale pid could kill or report the wrong process.

Policy:

- adoption requires command/start-time verification;
- cancellation after restart must be blocked unless adoption confidence is high.

### Scheduler Skill Escape Hatch

Risk:

- scheduler skills can run long enough to hide direct subprocess launches inside
  skill code.

Policy:

- scheduler and skill code must not spawn unregistered long-running OS
  subprocesses;
- long OS jobs must be created through `BackgroundJobManager`;
- tests should cover that scheduler integration returns a `job_id` instead of
  waiting for process completion.

### Shell Bypass

Risk:

- agents can still launch unmanaged long-running processes with `nohup`,
  trailing `&`, `setsid`, `disown`, `start`, or equivalent platform-specific
  detached-process patterns.

Policy:

- Phase 1 does not block these paths globally, because doing so before the
  manager is stable would create operator risk;
- Phase 1.5 should add detection in shell-facing paths and either reject the
  command or redirect the caller to `BackgroundJobManager.start(...)`;
- bypass detection must be conservative and explain the replacement command or
  API clearly;
- tests should cover at least `nohup`, trailing `&`, and scheduler-skill
  direct `Popen` examples.

## Implementation Roadmap

This is not the shortest path. It is the serious, durable path.

### Phase 0: Design And Tests Only

- Land this design document.
- Add test plan documents or failing/xfail design tests if desired.
- Lock the Phase 1 SQLite schema and manager public API before writing feature
  code.
- Define `/reboot` recovery expectations before adding Telegram commands.

### Phase 1: Local Manager, SQLite Store, Monitor, And Lifecycle

- Add `orchestrator/background_jobs.py`.
- Add a SQLite-backed local store.
- Add a kernel-owned function-layer service handle:
  `kernel.background_job_manager`.
- Add `service_manager.start_background_jobs()`,
  `stop_background_jobs()`, and `restart_background_jobs()`.
- Add unit tests for state transitions, store writes, log tailing, write-side
  log caps, policy denial, cancellation, orphan handling, restart/reboot
  recovery marking, and notification idempotency.
- Add local runtime service initialization in the function/service layer without
  moving supervision logic into protected core.
- Keep Remote, Workbench write APIs, and model-facing tools out of scope.
- Keep `/bg` command routing out of scope unless it is needed only as a thin
  manual smoke adapter; the manager API and tests are the source of truth.

Acceptance:

- start a local job;
- receive job id immediately;
- manager list/status/tail/cancel work through direct service tests;
- completion notification is sent;
- `/reboot` recovery is explicit;
- full restart marks unsafe running jobs explicitly.

### Phase 1.5: Launch Guardrails And Nagare Migration Plan

- Add conservative detection for unmanaged detached shell patterns such as
  `nohup`, trailing `&`, `setsid`, `disown`, and zero-wait process launch
  requests where those surfaces exist.
- Add tests that scheduler and skill code do not start unregistered
  long-running OS subprocesses.
- Define the migration path for `flow_trigger.py` so Nagare workflows can move
  from `_trigger_registry.jsonl` as lifecycle state to
  `BackgroundJobManager.start_from_flow(...)`.
- Keep existing Nagare JSONL as an audit/compatibility log during migration,
  not as the authoritative job lifecycle store.

Acceptance:

- common detached-process bypasses are either rejected with a clear message or
  redirected to Background Jobs;
- `flow_trigger.py` has an explicit migration contract;
- no model-facing tools are required for these guardrails.

### Phase 1b: Workbench API, Telegram Adapter, And Agent Terminal Events

- Add structured Workbench endpoints:
  - `POST /api/background-jobs`
  - `GET /api/background-jobs`
  - `GET /api/background-jobs/{job_id}`
  - `GET /api/background-jobs/{job_id}/tail`
  - `POST /api/background-jobs/{job_id}/cancel`
- Add tests for auth, agent scoping, status, and tail bounds.
- Add `/bg` Telegram command module as a thin adapter over the manager.
- Keep Telegram notification as a channel, not the source of truth.
- Add one-shot completion/failure event routing back to the responsible agent
  so terminal jobs can be summarized or continued after the original turn has
  ended.

Acceptance:

- Workbench can render a jobs dashboard without scraping transcripts.
- Telegram can start/list/status/tail/cancel without owning job semantics.
- terminal success/failure can wake the responsible agent once and produce a
  user-visible report without exposing the internal event payload.

### Phase 2: Write APIs, Cancellation, Retention, And Quotas

- Add Workbench notification preference endpoints.
- Add retention cleanup.
- Add per-agent and per-instance quotas in Layer 4 config.
- Add cross-agent/admin cancellation policy tests.

Acceptance:

- operator surfaces can cancel jobs safely;
- retention prevents unbounded workspace growth;
- quota enforcement is deterministic.

### Phase 3: Remote Background Jobs

- Add `background_jobs_v1` capability.
- Add Remote endpoints.
- Reuse `AuthLevel` classification but apply stricter long-running policy.
- Persist remote-side jobs under `~/.hashi-remote/background_jobs`.
- Add hchat/protocol completion delivery.
- Add durable remote pending-notification retry storage.
- Add Windows/WSL smoke gates before advertising reliable cancellation.

Acceptance:

- one HASHI instance can start a background job on another instance;
- completion can be observed even if the initiating chat turn ended.
- Remote execution uses the target instance's policy, not the initiator's
  policy.

### Phase 4: Tool Surface

- Add model-facing background job tools.
- Keep output compact and bounded.
- Audit every tool call.

Acceptance:

- agents can intentionally start and monitor background jobs without abusing
  `bash`.

### Phase 5: Enterprise Hardening

- Enterprise DB-backed job metadata if local SQLite is insufficient for the
  deployment profile.
- Org/project scoping.
- immutable audit records.
- per-project concurrency and quota.
- richer approval workflow for high-risk commands.
- optional SIEM/webhook event export.

## Test Strategy

Unit tests:

- job id generation;
- state transition legality;
- SQLite schema migration and idempotent writes;
- stdout/stderr tail bounds;
- write-side stdout/stderr byte caps;
- cancellation state changes;
- orphan process metadata when cancellation fails;
- timeout state changes;
- restart recovery marking;
- `/reboot` recovery semantics;
- notification idempotency;
- policy denial.

Integration tests:

- start `sleep`/short Python process and detect success;
- start failing command and detect failure;
- cancel long command;
- verify logs are written;
- verify Telegram/Workbench notification hooks are called through fakes;
- verify Workbench read-only API can query jobs without transcript scraping;
- verify Remote API refuses background jobs without capability/auth.

Regression tests:

- existing `background_mode` LLM tests still pass;
- existing `/terminal/exec` remains synchronous and short-lived;
- `/terminal/exec` does not accept background job semantics;
- scheduler tests still treat cron/heartbeat as trigger definitions;
- scheduler integration creates a job id instead of waiting for long OS process
  completion;
- Workbench `/api/admin/notify` remains compatible.

Manual smoke:

```bash
/bg run python -c "import time; print('start'); time.sleep(5); print('done')"
/bg list running
/bg status job_...
/bg tail job_...
```

Remote smoke:

```bash
POST /background/jobs
GET /background/jobs/{job_id}
GET /background/jobs/{job_id}/tail
```

## Open Decisions

1. Store format for Phase 1:
   - Decision: use SQLite for Phase 1 local/personal.
   - Rationale: concurrent-safe enough for this service, simple to query from
     Workbench, better for idempotent notification/cancel tracking, and still
     local-first.
   - JSON remains an export/backup format, not the authoritative live store.

2. Whether to support process adoption after restart in Phase 1:
   - Recommendation: mark as `abandoned_after_restart` first. Add adoption only
     after platform-specific verification exists.

3. Whether shell mode or argv mode is default:
   - Recommendation: support both, prefer argv for API/tool calls, allow shell
     for Telegram/operator convenience under policy.

4. Whether completion notification should inject a new agent queue item or send
   direct text:
   - Recommendation: direct text for operator notification; optional queue item
     only when the user explicitly wants the agent to interpret the result.

5. Whether Background Jobs should be part of Remote protocol manager or separate
   Remote service:
   - Recommendation: separate manager/service, protocol manager only handles
     correlation and delivery events.

6. Whether manager ownership violates minimal core:
   - Decision: no, if implemented as a function-layer service handle.
   - The kernel may own `background_job_manager` as a handle, but protected core
     should not contain process supervision, policy, or storage behavior.

7. Whether `/terminal/exec` should get `background=true`:
   - Decision: no. Keep `/terminal/exec` synchronous and short-lived. Use
     `background_jobs_v1` instead.

## Recommendation

Implement Background Jobs as a first-class managed subsystem:

- not a scheduler extension;
- not a `bash background=true` shortcut;
- not an in-memory process table;
- not a Remote-only feature.

The right long-term design is a durable, policy-governed job manager with
explicit lifecycle states, bounded logs, structured APIs, and notification
delivery as a separate concern.

This design gives HASHI a serious orchestration primitive that fits both its
personal assistant roots and its enterprise-grade Agent as Interface direction.
