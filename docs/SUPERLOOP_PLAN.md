# HASHI Superloop Plan

Status: draft architecture plan based on repo survey on 2026-05-15.

## 1. Purpose

HASHI already has two useful but different automation shapes:

- `/loop` for simple recurring work driven by heartbeat or cron semantics
- Nagare for stateful multi-step workflows with a clear start and end

The proposed `/superloop` is a third shape:

- long-running
- stateful
- potentially unbounded in step count
- able to pause, wait, resume, retry, and hand work across agents or instances
- driven by exit conditions rather than a single fixed terminal workflow path

This document does not propose replacing Nagare. It proposes using Nagare as a
sub-flow engine inside a higher-level superloop controller.

## 2. Product Definition

### 2.1 What `/superloop` is

`/superloop` should be a long-running orchestration layer that:

- records a shared workflow state outside any single agent session
- supports explicit waits such as sleep, human approval, file arrival, or peer reply
- can delegate work to local or remote agents via protocol/HChat
- can run one or more bounded Nagare child workflows as part of a larger loop
- keeps a shared taskboard and issue register for multi-agent coordination
- continues until an exit condition is satisfied or an operator aborts it

### 2.2 What `/superloop` is not

`/superloop` should not be:

- a larger `/loop`
- a replacement for Nagare's workflow runner
- a chat-memory trick inside one agent workspace
- a transport protocol of its own

## 3. Current Repo Findings

### 3.1 `/loop` exists, but it is intentionally lightweight

Relevant files:

- `orchestrator/flexible_agent_runtime.py`
- `orchestrator/runtime_command_binding.py`
- `orchestrator/scheduler.py`
- `tasks.json`

Current behavior:

- natural-language creation of recurring cron/heartbeat work
- small `loop_meta` state
- list/stop style controls

Current limitations relative to `/superloop`:

- no shared workflow state
- no explicit taskboard
- no issue register
- no cross-agent step ownership model
- no first-class wait registry
- no multi-stage recovery semantics

Conclusion:

- keep `/loop` for simple recurring jobs
- do not expand `/loop` into the superloop engine

### 3.2 Nagare already provides the best execution substrate

Relevant files:

- `nagare/engine/state.py`
- `nagare/engine/runner.py`
- `nagare/engine/artifacts.py`
- `nagare/logging/events.py`
- `nagare/protocols/notifier.py`
- `nagare/cli.py`
- `docs/NAGARE_FLOW_SYSTEM.md`

Capabilities already present:

- atomic `state.json` writes
- persisted step state
- explicit workflow lifecycle
- pause/stop signal files
- artifact storage
- recoverable run history
- notifier abstraction
- event logging

Conclusion:

- Nagare should remain the bounded sub-workflow engine
- `/superloop` should sit above Nagare, not inside one agent conversation

### 3.3 Cross-agent transport is already good enough

Relevant files:

- `tools/hchat_send.py`
- `tools/protocol_send.py`
- `tools/remote_file_transfer.py`
- `remote/api/server.py`

Capabilities already present:

- cross-instance protocol messaging
- HChat routing
- attachment send
- file transfer
- shared-token remote auth

Conclusion:

- `/superloop` should reuse these tools for actions and handoffs
- transport is not the missing layer; shared orchestration state is

### 3.4 Register-style persistence patterns already exist

Relevant files:

- `orchestrator/ticket_manager.py`
- `tickets/open/`

Useful patterns already present:

- JSON register files
- deterministic state moves
- automatic local diagnostics capture

Conclusion:

- taskboard and issue register should follow the same general persistence style

### 3.5 Existing per-agent workspace state must remain separate

Relevant files:

- `workspaces/<agent>/state.json`
- `workspaces/<agent>/recent_context.jsonl`
- `workspaces/<agent>/handoff.md`
- `workspaces/<agent>/skill_state.json`

Conclusion:

- these are agent-local continuity helpers
- they must not become the shared source of truth for superloop state

## 4. Design Principles

1. Keep the core minimal.
2. Reuse Nagare for bounded workflows instead of duplicating its engine.
3. Make shared state file-based, inspectable, and atomic.
4. Use transport tools only for actions; never treat messages as the source of truth.
5. Prefer append-only event logs plus a materialized current state.
6. Make pause, wait, resume, and abort explicit first-class concepts.
7. Preserve backward compatibility for `/loop`, Nagare, HChat, and remote tools.
8. Add comprehensive logging and audit fields at every orchestration boundary.

## 5. Recommended Architecture

### 5.1 Layer model

```text
/superloop command surface
  -> superloop controller
      -> shared superloop state store
      -> wait engine
      -> taskboard / issue register
      -> transport actions (HChat / protocol / file / attachments)
      -> Nagare child runs when a bounded workflow segment is needed
      -> scheduler wakeups for resume / polling / watchdog
```

### 5.2 Core distinction

Nagare:

- bounded
- workflow-shaped
- DAG / step execution oriented
- clear run lifecycle

Superloop:

- loop-shaped
- exit-condition driven
- may spawn multiple Nagare child runs over time
- may wait days between actions
- may coordinate multiple agents on one shared board

## 6. Data Model Proposal

Create a new top-level directory:

```text
superloops/
  <loop_id>/
    state.json
    events.jsonl
    taskboard.json
    issues.json
    waits.json
    artifacts/
    children/
```

### 6.1 `state.json`

Purpose:

- current materialized state for operators and controller logic

Suggested fields:

- `loop_id`
- `title`
- `status`
- `owner_agent`
- `participants`
- `created_at`
- `updated_at`
- `started_at`
- `ended_at`
- `current_phase`
- `current_step`
- `next_action`
- `exit_condition`
- `paused`
- `pause_reason`
- `active_wait_id`
- `child_runs`
- `stats`

### 6.2 `events.jsonl`

Purpose:

- append-only audit trail

Suggested event types:

- `loop.created`
- `loop.started`
- `step.added`
- `step.completed`
- `step.failed`
- `wait.entered`
- `wait.satisfied`
- `loop.paused`
- `loop.resumed`
- `task.assigned`
- `issue.opened`
- `issue.resolved`
- `child_run.started`
- `child_run.completed`
- `message.sent`
- `file.sent`
- `attachment.sent`
- `loop.aborted`
- `loop.completed`

### 6.3 `taskboard.json`

Purpose:

- shared coordination board for multi-agent work

Suggested task fields:

- `task_id`
- `title`
- `description`
- `status`
- `owner_agent`
- `owner_instance`
- `depends_on`
- `priority`
- `created_at`
- `updated_at`
- `artifact_refs`
- `notes`

### 6.4 `issues.json`

Purpose:

- shared issue/risk register for blocked or uncertain items

Suggested fields:

- `issue_id`
- `title`
- `status`
- `severity`
- `opened_by`
- `opened_at`
- `assigned_to`
- `related_task_ids`
- `resolution`

### 6.5 `waits.json`

Purpose:

- explicit machine-readable wait states

Suggested wait types:

- `sleep_until`
- `await_human`
- `await_hchat_reply`
- `await_protocol_reply`
- `await_file`
- `await_remote_online`
- `await_issue_resolution`
- `await_child_run`

## 7. Exact Reuse Plan

### 7.1 Reuse directly

- `nagare/engine/state.py`
  - reuse atomic state-writing pattern and lifecycle discipline
- `nagare/engine/runner.py`
  - reuse as the engine for bounded child workflows
- `nagare/engine/artifacts.py`
  - reuse artifact management concepts and path conventions where practical
- `nagare/logging/events.py`
  - reuse event logging style and field discipline
- `nagare/protocols/notifier.py`
  - reuse notifier abstraction for superloop notifications
- `orchestrator/scheduler.py`
  - reuse as the wake-up mechanism for waits, retries, and watchdogs
- `tools/hchat_send.py`
  - reuse for cross-agent coordination sends
- `tools/protocol_send.py`
  - reuse for explicit protocol deliveries and structured replies
- `tools/remote_file_transfer.py`
  - reuse for cross-instance artifact/file movement
- `orchestrator/ticket_manager.py`
  - reuse register-style persistence ideas, not the ticket product semantics themselves

### 7.2 Reuse carefully, but do not make them the backbone

- `orchestrator/flexible_agent_runtime.py`
  - reuse command wiring patterns and runtime integration points only
- `orchestrator/runtime_command_binding.py`
  - reuse for registering `/superloop`
- `orchestrator/runtime_jobs.py`
  - reuse job list/transfer/wake semantics if a superloop needs scheduled wakeups
- `workspaces/<agent>/recent_context.jsonl`
  - reuse as optional context source when composing handoffs, not as workflow truth
- `workspaces/<agent>/handoff.md`
  - reuse as optional human-readable summary output, not as controller state

### 7.3 Keep for historical compatibility only

- `flow/engine/*`
- `flow/flow_cli.py`
- `flow/flow_trigger.py`

These still document useful patterns, but `nagare/engine/*` should be treated as
the canonical execution stack for new work.

## 8. New Modules to Add

Recommended new modules:

- `orchestrator/superloop_manager.py`
  - top-level lifecycle API
- `orchestrator/superloop_store.py`
  - atomic read/write and indexing for superloop files
- `orchestrator/superloop_runner.py`
  - progression engine that decides the next action
- `orchestrator/superloop_waits.py`
  - wait registry and wake condition evaluation
- `orchestrator/superloop_taskboard.py`
  - taskboard CRUD and ownership logic
- `orchestrator/superloop_issues.py`
  - issue register CRUD and linking
- `orchestrator/superloop_nagare_adapter.py`
  - starts and tracks Nagare child runs
- `orchestrator/superloop_commands.py`
  - command parsing helpers if command logic should stay out of the large runtime file

Recommended new docs and tests:

- `docs/SUPERLOOP_PLAN.md`
- `tests/test_superloop_store.py`
- `tests/test_superloop_waits.py`
- `tests/test_superloop_taskboard.py`
- `tests/test_superloop_nagare_adapter.py`
- `tests/test_superloop_commands.py`

## 9. Command Surface Proposal

Suggested initial operator commands:

- `/superloop start <spec>`
- `/superloop status [id]`
- `/superloop list`
- `/superloop pause <id>`
- `/superloop resume <id>`
- `/superloop abort <id>`
- `/superloop next <id>`
- `/superloop task add <id> <text>`
- `/superloop issue add <id> <text>`
- `/superloop wait <id> <condition>`
- `/superloop log <id>`

The command surface should be a thin operator layer over structured state, not a
place where the main orchestration logic lives.

## 10. Boundaries: What Not to Touch

### 10.1 Do not overload `/loop`

Reason:

- `/loop` is currently understandable and operationally small
- turning it into a long-running state machine would blur two distinct products

### 10.2 Do not store shared superloop truth in `workspaces/<agent>/state.json`

Reason:

- that file is agent-local runtime state
- it should remain hot-reboot friendly and agent-scoped

### 10.3 Do not build a second workflow engine beside Nagare

Reason:

- the repo already contains old `flow/engine/*` and current `nagare/engine/*`
- adding a third execution core would multiply ambiguity and maintenance cost

### 10.4 Do not make HChat the state layer

Reason:

- transport is lossy as an orchestration truth source
- state must remain in structured, replayable files

### 10.5 Do not require Remote core changes for v1

Reason:

- current protocol/HChat/file/attachment tools are already sufficient
- `/superloop` should compose existing transport rather than widening protocol scope first

## 11. Logging Requirements

Superloop should log at least:

- loop id
- event id
- actor agent
- actor instance
- action type
- target agent / instance
- wait type
- wait satisfaction source
- child Nagare run id
- task id / issue id references
- artifact refs
- timestamps and duration
- failure code and message

This logging should exist both in:

- `events.jsonl`
- runtime logs where relevant

## 12. Phased Implementation Draft

### Phase 0: state layer only

Build:

- `superloops/<loop_id>/state.json`
- `events.jsonl`
- list/status helpers

Do not build yet:

- Nagare child-run integration
- taskboard
- issue register
- waits beyond a minimal pause/resume model

Exit gate:

- create, inspect, pause, resume, abort one superloop with atomic state updates

### Phase 1: wait engine + scheduler wakeup

Build:

- `waits.json`
- explicit wait types
- scheduler-based wake/check cycle

Exit gate:

- loop can sleep and resume on time without manual intervention

### Phase 2: taskboard + issue register

Build:

- `taskboard.json`
- `issues.json`
- owner/claim/dependency model

Exit gate:

- multiple agents can coordinate one loop through shared files and event logs

### Phase 3: Nagare child-run integration

Build:

- adapter from superloop steps to Nagare child runs
- child run tracking under `children/`

Exit gate:

- one superloop can run a bounded Nagare workflow and continue after it completes

### Phase 4: remote-first multi-agent loops

Build:

- HChat/protocol/file/attachment orchestration helpers
- stronger wait conditions for peer replies and remote artifacts

Exit gate:

- one loop can coordinate local and remote agents across instances

## 13. Recommended v1 Scope

The smallest good `/superloop` is:

- create/list/status/pause/resume/abort
- file-based shared state
- append-only event log
- explicit wait model
- optional HChat/protocol action steps

The smallest good `/superloop` is not:

- a fully visual workflow editor
- a general BPMN system
- a replacement for Nagare DAG execution

## 14. Final Recommendation

Build `/superloop` as a new orchestration layer that reuses:

- Nagare for bounded workflow execution
- scheduler for wakeups
- HChat/protocol/file transfer for actions
- register-style JSON persistence for shared state

Do not evolve `/loop` into this.

Do not fork execution semantics away from Nagare.

Do not mix agent-local runtime state with shared superloop state.

If implemented with those boundaries, `/superloop` can become the long-running,
multi-agent control plane for HASHI without bloating the runtime core or
creating a third workflow engine.
