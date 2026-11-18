# HERV3 Small-Model Daily & Developer Toolkit

Status: **experimental branch; local validation and PR review required**.
Functional owner: **PAO Tool Registry** (capability/admission/execution); HERV3 Provider
only projects compact tool definitions. Engineering placement: **Layer 2 Functions**.
No protected Core, production process, Remote or instance config changes.

## Why

Qwen/Gemma-class models work more reliably with small, structured, higher-level
operations instead of generating large PowerShell/Python commands. The existing
large tool catalogue remains available but need not be sent with every prompt.

| Tool | Purpose |
|---|---|
| `tool_find` | Find and reveal existing allowed tools, e.g. `{"pack":"office_basic"}` |
| `project_locate` | Locate SOK/other projects by name or Git remote, within a known Workzone root |
| `code_search` | Fast ripgrep source search with partial results and a tool-local deadline |
| `git_inspect` | Read status, branches, log, diff stat and sanitised remotes |
| `file_manage` | Create directory, copy or move with explicit overwrite behaviour |
| `text_replace` | Exact-count UTF-8 text replacement, preview optional |
| `data_inspect` | Structured CSV, TSV, JSON, JSONL preview |

Existing `file_search`, `log_query`, `shell`, `apply_patch`,
`workspace_inspect`, `verification_run`, Office, Browser and all configured
tools remain available. The toolkit does not replace the separate scoped-search
repair under development.

## Tool-local timeout, no Agent timeout

`code_search` defaults to a **15-second tool call** (parameter `timeout_s` up
to 60 seconds). On deadline, it returns `partial=true` and `stop_reason` so the
Agent can narrow scope, switch strategy or report an evidence gap. It does not
end the HERV3 Turn or cap tool loops. `project_locate` and `git_inspect` also
have individual bounded call budgets, never an overall Agent budget.

## Opt-in compact presentation

The branch preserves the original full-tool behaviour by default. For a single
**non-production** test backend row inside ignored local `agents.json`:

~~~json
{
  "tools": {
    "herv3_tool_presentation": {
      "mode": "on_demand"
    }
  }
}
~~~

This is a standard backend row `tools` setting. Global `default_tools` can
also carry this option, but **do not enable globally for initial tests**.

Instances whose allowed_tools uses the * wildcard must additionally list the
toolkit tools explicitly (for example tool_find, project_locate,
code_search, git_inspect, file_manage, text_replace, data_inspect).
A wildcard alone does not adopt the new tools, so instances without this
configuration keep their prior behaviour unchanged (the same precedent as
media_read and vision_inspect).

In HERV3 on-demand mode, the initial model payload offers a small starter set
and a few goal-matched suggestions. A `tool_find` call activates matching
already-allowed tool schemas on the next provider call. This is a
request-local presentation view, **not** a new allowlist or permission gate;
every invocation still crosses the original Tool Registry, Workzone boundaries,
availability, fingerprint checks and audit. A larger HERV3 model can opt in too.

Example:

1. `project_locate({"name":"SOK","root":"D:/Projects"})` finds candidate paths.
2. `git_inspect({"operation":"status","path":"<returned folder>"})` checks Git.
3. For Excel, `tool_find({"pack":"office_basic"})`, then call existing Office tools.
4. For a test, `tool_find({"pack":"verification"})`, then existing verification tools.

### Scope and behaviour

- Reads use frozen read Workzones; mutations use writable Workzones.
- Repository discovery does not follow symlinked directories.
- Git inspection is read-only, with credential-stripped remote output.
  Existing shell retains git commit/push; no second Git identity system.
- Exact text replacements require the expected literal occurrence count.
- Source search uses `rg` when installed, otherwise a bounded Python literal
  fallback; regex requires `rg`.
- Failures use typed JSON `ok=false`; Registry recognises these as tool errors.
- No new runtime dependencies, background worker, permissions, lifecycle process
  or hard Agent ceiling.

## Local validation

See [HERV3 Toolkit Local Handover](HERV3_TOOLKIT_LOCAL_HANDOVER.md).
Test on an isolated HASHI copy and Workzones using Qwen/Gemma4 plus one large
HERV3 model; check Windows/WSL paths, Office discovery, cancellation, no
permission expansion, and unchanged protected Core. Do not merge or deploy
before Barry reviews the PR.

## Files

- `tools/developer_toolkit_schemas.py`: concise function schemas.
- `tools/developer_toolkit.py`: PAO Function implementations.
- `tools/tool_presentation.py`: per-request schema discovery view.
- `tools/registry.py`: existing admission/dispatch integration.
- `adapters/her_v2_provider.py`: opt-in HERV3 view.
- `tests/test_herv3_developer_toolkit.py`: local regression fixtures.
