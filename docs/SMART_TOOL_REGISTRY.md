# Smart Tool Registry

HASHI uses one deterministic layer around its existing tools. It is not a model
or a semantic task planner. It may, however, reject a mechanically unsafe tool
shape before execution and return typed guidance so the HERV3 main model can
choose a safer alternative inside the same loop.

```text
HERV3 main loop -> Smart Tool admission -> Tool Registry -> Tool
                         |                 |             |
                         +--------------- Ledger <-------+
```

The Registry keeps existing permission checks, performs bounded deterministic
admission checks, invokes the original tool, adapts the result, adds a soft
repeat warning when appropriate, and appends one ledger row. Permission and
process execution remain PAO/Tool Registry responsibilities; HERV3 owns how it
responds to `needs_replan` and chooses the suggested alternative. The retained
`needs_replan` result name means “choose another action”; it does not invoke a
Replanning stage.

## Safe text and log search

`log_query` is the first Smart Tool with a safer specialised execution path. It
searches one authorised UTF-8 text, log, JSONL, or NDJSON file for escaped
literal terms in fixed-size chunks. It never evaluates a caller-provided regex,
never materialises a complete record, and returns only bounded excerpts.

Before `shell` or its legacy `bash` alias starts a process, Smart Tool admission
recognises line-oriented `grep`/`ripgrep` forms that combine only-matching
output, wildcard context expansion, or an unbounded result with a probed large
record. The process is not spawned. HER receives `status: needs_replan`, exact
reason data, and `suggested_tool: log_query`. This is a narrow deterministic
safety rule, not a general shell parser or a model-level task decision.

## Tool profiles

Every registered tool inherits one of six shared behaviours:

| Profile | Intended use | Repeat behaviour |
| --- | --- | --- |
| `query` | Read, search, inspect | Warn after the same result is seen three times |
| `poll` | Observe changing job or UI state | Allow repeats and suggest a longer interval |
| `verify` | Run a correctness or validation check | Warn after the same input and result repeat |
| `idempotent_action` | Apply or re-check a desired state | Always execute; the adapter decides `changed` or `no_change` |
| `side_effect_action` | Send, create, delete, or otherwise cause an external effect | Execute, but warn when the same arguments already succeeded |
| `generic` | Tools whose effect cannot be predicted safely | Observe the actual return only |

Each tool has a name, semantic version, profile, and short natural-language
description. A legacy adapter is declared only when deterministic translation
is required.

## Executor-visible result

When enabled, every completed call returns exactly five top-level fields:

```json
{
  "status": "success",
  "effect": "observed",
  "data": {},
  "error": null,
  "warning": null
}
```

`status` is one of `success`, `failed`, `unavailable`, `partial`, or
`needs_replan`. The latter proves that the requested Tool was not executed and
that HER should choose the typed safer alternative. `effect` is one of
`observed`, `changed`, `no_change`, or `unknown`. Hashes, versions, timestamps,
and repeat counters stay out of the Executor context.

A failed action defaults to `effect: unknown` unless the adapter can prove that
no change occurred. For example, a rejected patch and a missing Scheduler
gateway are deterministic `no_change` outcomes.

## Ledger

Each completed invocation appends one JSON line containing:

```text
timestamp, task_id, call_id, stage, model, tool, tool_version,
args_hash, status, effect, error_code, duration_ms, result_hash, repeat_count
```

There are no requested/started/classified sub-events. Composite tools remain a
single top-level call. Existing canonical security audit evidence is retained;
the older duplicate tool-action log is disabled while Smart Tool Registry is
enabled.

## Repeat rules

Within one task, the third consecutive call with the same tool, arguments, and
result receives a warning. Poll tools receive an unchanged-state/backoff
warning. A repeated successful side-effect action receives an intent warning.
Repeat rules do not block or short-circuit execution. Deterministic admission
rules may do so before execution when they can name a concrete safer Tool.

The first version deliberately does not infer semantic novelty, causal
contribution, task success, or evidence quality.

## Configuration

```json
{
  "smart_registry": {
    "enabled": true,
    "ledger_path": "tool_ledger.jsonl",
    "repeat_threshold": 3,
    "shell_text_search_guard": true,
    "large_record_bytes": 1000000,
    "file_probe_bytes": 33554432,
    "foreground_timeout_seconds": 1800
  }
}
```

The feature is compatibility-gated and disabled when this block is absent.
`foreground_timeout_seconds` is an explicit instance safety fuse for one
foreground subprocess; it is deliberately generous and does not bound a HER
Turn, stage, Tool loop, or managed background job. When absent, omission of a
caller timeout retains the no-default-deadline behavior.

## Search execution budgets (2026-11-17)

Local search tools carry recoverable per-call execution budgets. They are tool
budgets, never Agent, Run, task or conversation limits:

| Operation | Engineering default | Override |
| --- | --- | --- |
| `file_search` | 45 s | per-call `timeout_seconds` argument or `tool_options.file_search.timeout_seconds` |
| `file_list` nonrecursive | 15 s | same keys |
| `file_list` recursive | 45 s | same keys |
| `log_query` | 45 s | same keys |
| `repo_discover` | 30 s | same keys |

Precedence: an explicit per-call `timeout_seconds` request wins; then the
configured `timeout_seconds` (including the Smart Registry foreground fuse);
then the per-operation default. A configured `timeout_max_seconds` caps
per-call requests only. Existing installations with explicit search settings
keep their exact behaviour; installations without any deadline configuration
now receive the defaults above instead of an unbounded scan.

On reaching its budget the scanner stops scheduling new work, serializes its
partial matches, truthful coverage and continuation state, and returns them.
The parent waits a short cleanup grace (default 2 s, configurable via
`tool_options.file_search.cleanup_grace_seconds`) for that serialization;
only if the worker fails to finish is the owned process tree terminated, and
the result then explicitly reports `continuation_lost` without inventing a
cursor. A deadline stop is never reported as complete coverage, and a cursor
is never a command to continue: the model decides whether to continue, narrow
the roots, refine the query, switch tools, or stop.

`repo_discover` identifies local Git checkouts by directory name, remote name
or remote URL without scanning repository contents (`.git` directories,
worktree `gitdir` files, remotes and HEAD branch only). It is read-only, uses
the existing read scope, and performs no network access.
