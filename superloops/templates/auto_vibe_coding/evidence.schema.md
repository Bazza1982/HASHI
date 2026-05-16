# Auto Vibe Coding Evidence Schema

Use this schema for the loop evidence artifact. Keep entries short, concrete,
and command-backed.

## Header

```text
loop_id:
repo:
branch:
started_at:
orchestrator:
worker:
reviewer:
user_request:
exit_condition:
```

## Baseline

```text
git_status:
head_sha:
dirty_files_user_owned:
in_scope:
out_of_scope:
known_risks:
```

## Worker Report

```text
agent:
assigned_scope:
files_changed:
behavior_changed:
commands_run:
command_results:
failures:
residual_risks:
handoff_summary:
```

## Orchestrator Integration

```text
files_inspected:
integration_decisions:
conflicts_or_overlap:
checks_run:
check_results:
live_checks_required: yes|no
live_check_results:
```

## Reviewer Report

```text
agent:
review_surface:
blockers:
non_blockers:
test_gaps:
acceptance_risk:
verdict: pass|pass_with_risk|fail
```

## Blocker Log

```text
blocker_id:
reported_by:
severity:
file_or_behavior:
fix_owner:
fix_summary:
retest_command:
retest_result:
reviewer_confirmed: yes|no|not_required
```

## Commit And Close

```text
staged_files:
commit_sha:
commit_message:
exit_condition_met: yes|no
open_risks:
next_step:
closed_at:
```
