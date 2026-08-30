# Smart Tool Registry

HASHI uses one small, deterministic layer around its existing tools. It is not
an intelligence service and it does not make task decisions.

```text
Executor -> Smart Tool Registry -> Tool
                 |                 |
                 +---- Ledger <----+
```

The Registry keeps existing permission checks, invokes the original tool,
adapts the result, adds a soft repeat warning when appropriate, and appends one
ledger row. Existing tools do not need to be rewritten.

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

`status` is one of `success`, `failed`, `unavailable`, or `partial`. `effect` is
one of `observed`, `changed`, `no_change`, or `unknown`. Hashes, versions,
timestamps, and repeat counters stay out of the Executor context.

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
No rule blocks or short-circuits execution.

The first version deliberately does not infer semantic novelty, causal
contribution, task success, or evidence quality.

## Configuration

```json
{
  "smart_registry": {
    "enabled": true,
    "ledger_path": "tool_ledger.jsonl",
    "repeat_threshold": 3
  }
}
```

The feature is compatibility-gated and disabled when this block is absent.
