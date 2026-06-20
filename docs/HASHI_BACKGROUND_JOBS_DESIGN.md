# HASHI Background Jobs Design

Status: draft
Date: 2026-06-20
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

## Existing Code Review

This design is based on a review of the current HASHI code paths that overlap
with background execution.

### Runtime Queue And LLM Background Mode

Relevant files:

- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/runtime_pipeline.py`
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

Current gaps:

- This background path is only for backend LLM generation tasks, not arbitrary
  OS processes.
- It is in-memory. A HASHI restart cannot rebind to a detached LLM task.
- There is no durable job id, no command metadata, no stdout/stderr log model,
  no status query API, and no cancellation surface.
- `_background_tasks` is an `asyncio.Task` set, not a process registry.
- The existing notification path can be reused, but it is not sufficient as the
  persistence or supervision layer.

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
mutate `/terminal/exec` semantics in a backward-incompatible way. A compatibility
extension can allow `/terminal/exec` to accept `background=true`, but the
canonical API should be explicit.

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

Design implication:

Add explicit tool functions only after the manager exists:

- `background_job_start`
- `background_job_status`
- `background_job_tail`
- `background_job_cancel`
- `background_job_list`

Do not add `background=true` to generic `bash` first; that would blur security,
audit, and ownership boundaries.

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
├── BackgroundJobPolicy
└── BackgroundJobApiAdapter
```

### Component Responsibilities

`BackgroundJobManager`

- public service API used by runtime commands, Workbench, Remote, scheduler, and
  tools;
- coordinates validation, persistence, process start, status transitions,
  monitor registration, and notifications.

`BackgroundJobStore`

- durable JSON or SQLite-backed store;
- records job metadata, lifecycle state, timestamps, return code, pid, paths,
  owner, origin, and notification policy;
- writes atomically;
- supports startup recovery.

`BackgroundProcessRunner`

- starts OS processes;
- creates stdout/stderr log files;
- uses process groups where supported;
- handles environment filtering;
- avoids shell when argv mode is available;
- supports shell mode only when policy allows it.

`BackgroundJobMonitor`

- monitors running jobs;
- detects process exit;
- records return code and duration;
- enforces timeout and idle-timeout rules;
- marks jobs abandoned on restart when rebind is unsafe;
- handles log rollover and last-output snapshots.

`BackgroundJobNotifier`

- emits structured completion events;
- sends Telegram/chat text through runtime delivery helpers;
- emits Workbench events;
- optionally sends hchat/protocol replies when a job was remote-originated.

`BackgroundJobPolicy`

- validates command, cwd, environment, actor, auth level, source, and profile;
- enforces personal vs enterprise behavior;
- owns concurrency limits and dangerous-command blocks.

`BackgroundJobApiAdapter`

- exposes Workbench and Remote APIs;
- converts HTTP payloads into manager calls;
- never starts processes directly.

### Layer Placement

Following `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`:

- `orchestrator/background_jobs.py`: Layer 2 function service.
- `orchestrator/commands/background_jobs.py`: Layer 2 command surface.
- `orchestrator/workbench_api.py` additions: Layer 2 API surface.
- `remote/background_jobs_client.py` or `remote/api/server.py` endpoint wiring:
  Remote function layer.
- local job state under workspace or `~/.hashi/background-jobs`: Layer 4 state.
- enterprise governance integration: enterprise function layer.

No protected core change should be required for the first implementation unless
the kernel service lifecycle needs a new owned service handle.

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
notification_failed
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

`notification_failed` should usually be a secondary flag, not the primary
terminal state, because the process may have succeeded even if notification
delivery failed.

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
    "combined_path": "background_jobs/job_.../combined.log",
    "last_output_excerpt": ""
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
workspaces/<agent>/background_jobs/
  index.json
  job_<id>/
    meta.json
    stdout.log
    stderr.log
    combined.log
```

Remote sidecar default:

```text
~/.hashi-remote/background_jobs/<instance_id>/
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
5. Store writes `cancelled` or `failed` if termination fails.
6. Notifier sends cancellation summary.

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

## Public Surfaces

### Telegram Commands

Suggested commands:

```text
/bg run <command>
/bg list [running|recent|failed|all]
/bg status <job_id>
/bg tail <job_id> [stdout|stderr|combined]
/bg cancel <job_id>
/bg notify <job_id> on|off
```

The command module should live under `orchestrator/commands/background_jobs.py`
and register through the existing `RuntimeCommand` mechanism.

Telegram output must be concise. Full logs should not be dumped into chat.
`tail` should default to a small bounded excerpt.

### Workbench API

Suggested endpoints:

```text
POST /api/background-jobs
GET  /api/background-jobs?agent=zelda&state=running
GET  /api/background-jobs/{job_id}
GET  /api/background-jobs/{job_id}/tail?stream=combined&lines=80
POST /api/background-jobs/{job_id}/cancel
POST /api/background-jobs/{job_id}/notify
```

Workbench should treat the job store as the source of truth and should render
structured job metadata, not parse chat notifications.

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

## Security And Governance

### Command Policy

Background jobs are more dangerous than short shell calls because they persist
after the turn ends. Policy must be stricter than normal `bash`.

Personal profile:

- default allowed under configured workspace;
- shell mode allowed if the agent already has shell/builtin permission;
- dangerous patterns blocked by existing and new policy rules;
- max concurrent jobs per agent defaults to 2.

Enterprise profile:

- disabled by default until explicitly enabled;
- requires project workspace scope;
- command cwd must pass `ExecutionScope`;
- environment allowlist required;
- audit event required for start, cancel, completion, and failure;
- L3/L4 actions require explicit approval or remain blocked.

### Process Groups

On POSIX:

- start with a new process group/session when possible;
- cancel the process group, not just the parent pid;
- record pgid.

On Windows:

- use Windows-specific job object or process-tree termination when available;
- otherwise mark cancellation semantics as best-effort.

### Log Policy

Logs must be bounded:

- max bytes per stream;
- tail excerpts in notifications;
- no unlimited Telegram output;
- optional redaction hooks for secrets;
- log paths scoped under job-owned directories.

### Ownership

A job has:

- owning agent;
- owning instance;
- origin source;
- chat id or Workbench session when applicable;
- optional remote correlation.

Cancellation should require the same actor class or an operator/admin channel.

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
- expose status via Remote API.

### Log Explosion

Risk:

- batch jobs can write unlimited output.

Policy:

- use bounded log files or rotation;
- track truncation in metadata;
- notification includes only bounded tail.

### Duplicate Notifications

Risk:

- restart/retry can send completion twice.

Policy:

- store notification delivery idempotency keys;
- completion delivery should check `notification.delivered` and delivery target.

### PID Reuse

Risk:

- reusing a stale pid could kill or report the wrong process.

Policy:

- adoption requires command/start-time verification;
- cancellation after restart must be blocked unless adoption confidence is high.

## Implementation Roadmap

This is not the shortest path. It is the serious, durable path.

### Phase 0: Design And Tests Only

- Land this design document.
- Add test plan documents or failing/xfail design tests if desired.
- Decide store format: JSON first for personal, SQLite/Postgres later for
  enterprise.

### Phase 1: Local Manager, Store, And Commands

- Add `orchestrator/background_jobs.py`.
- Add unit tests for state transitions, atomic writes, log tailing, policy
  denial, cancellation, restart recovery marking.
- Add `/bg` command module.
- Add local runtime service initialization in the function/service layer.
- Keep Remote and model tools out of scope.

Acceptance:

- start a local job;
- receive job id immediately;
- list/status/tail/cancel work;
- completion notification is sent;
- restart marks unsafe running jobs explicitly.

### Phase 2: Workbench API

- Add structured Workbench endpoints.
- Add tests for auth, agent scoping, status, tail bounds, cancellation.
- Keep Telegram notification as a channel, not the source of truth.

Acceptance:

- Workbench can render a jobs dashboard without scraping transcripts.

### Phase 3: Remote Background Jobs

- Add `background_jobs_v1` capability.
- Add Remote endpoints.
- Reuse `AuthLevel` classification but apply stricter long-running policy.
- Persist remote-side jobs under `~/.hashi-remote/background_jobs`.
- Add hchat/protocol completion delivery.

Acceptance:

- one HASHI instance can start a background job on another instance;
- completion can be observed even if the initiating chat turn ended.

### Phase 4: Tool Surface

- Add model-facing background job tools.
- Keep output compact and bounded.
- Audit every tool call.

Acceptance:

- agents can intentionally start and monitor background jobs without abusing
  `bash`.

### Phase 5: Enterprise Hardening

- DB-backed job metadata.
- Org/project scoping.
- immutable audit records.
- per-project concurrency and quota.
- richer approval workflow for high-risk commands.
- optional SIEM/webhook event export.

## Test Strategy

Unit tests:

- job id generation;
- state transition legality;
- atomic store writes;
- stdout/stderr tail bounds;
- cancellation state changes;
- timeout state changes;
- restart recovery marking;
- notification idempotency;
- policy denial.

Integration tests:

- start `sleep`/short Python process and detect success;
- start failing command and detect failure;
- cancel long command;
- verify logs are written;
- verify Telegram/Workbench notification hooks are called through fakes;
- verify Remote API refuses background jobs without capability/auth.

Regression tests:

- existing `background_mode` LLM tests still pass;
- existing `/terminal/exec` remains synchronous and short-lived;
- scheduler tests still treat cron/heartbeat as trigger definitions;
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
   - JSON is simpler and matches current local state patterns.
   - SQLite is safer for concurrent updates and future Workbench queries.
   - Recommendation: JSON for first personal/local implementation only if all
     writes are atomic and manager serializes updates; SQLite for enterprise.

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
