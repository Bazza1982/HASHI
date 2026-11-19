# Smart Tool Registry

The request-scoped tool_catalog lists only already-authorized and currently available tools, with a query and paged continuation. Ollama may add returned tool schemas on a later turn; the Registry applies the active request grant again before advertising or execution. Managed execution does not expose tool_catalog until its supervised channel is qualified. This discovery mechanism does not grant additional tools.

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

## Resumable file reads

`file_read` keeps its line offset and page-size inputs. When a page ends before
EOF, it returns a `next_cursor`; pass that cursor with the same authorized path
to continue, including inside one oversized UTF-8 line. A cursor is not a read
grant: the path is checked again. File identity and a SHA-256 of the exact bytes
are verified for each continuation. If the file changes, restart the read.
Checksumming reads the complete file on each continued page, so very large files
can be slow; literal `log_query` remains available for targeted log searches.

## Retained tool-result continuations

When a Run-bound plain or structured text result exceeds the model projection
budget, the original text is retained in a private Run-bound disk store and
the projected result supplies a reference. `tool_output_read` reads it in pages from offset
zero, reports the exact SHA-256 and completeness, and verifies the original
Run binding. Structured image blocks remain unchanged. An incomplete projection
and its effect receipt explicitly report `complete_content=false`.

Web fetch streams the response and HTML conversion through temporary files.
With a bound Run, the converted text is retained for paging unless the caller
explicitly supplies a character limit. A standalone call without a Run binding
still returns its complete converted result in memory. The HTML parser's
unconsumed input is budgeted at 8 Mi characters by default;
`HASHI_WEB_FETCH_PARSER_MEMORY_CHARS` can change this budget or disable it with
zero. Exceeding it returns an error rather than a partial success. Streaming
does not guarantee bounded memory for all HTTP decompression or optional
converter internals. Very long automatic links can use equivalent Markdown link
formatting rather than the previous short form.

Legacy screenshot strings with more frames or image bytes than fit in one MCP
page are retained in the same Run-bound store.
`tool_output_read` returns image blocks, and its `offset`/`next_offset` count
source frames rather than text characters for these references. Invalid frames
are reported and prevent a complete-coverage claim. Each page defaults to six
frames and 4,000,000 normalized image bytes; operators may configure
`HASHI_MCP_IMAGE_PAGE_COUNT` and `HASHI_MCP_IMAGE_PAGE_BYTES`. These are paging
budgets, not negotiated provider limits. Existing image decode/source limits and
the owner-proxy response limit still apply. Already-structured image results
continue through their existing path.

When instance state is available, continuations use a protected private store,
survive Registry and Worker replacement, and are removed after the canonical
Run finishes or is cancelled. Shell and verification stdout/stderr are spooled
privately and can be retrieved in full without holding complete output in memory.
If a shell command times out, the captured output can still be paged while the
Run remains active. Cancelling the Run closes its temporary spools.
Workspace inspection spools full Git status, Git diff, and external-search output
before projecting a preview; continuation pages recover the selected stream.
Without a bound Run or available storage, omitted output is explicitly reported.
The developer-toolkit Git inspector uses the same continuation for status,
branches, log, diff and credential-redacted remote addresses. Its log limit
follows the requested count without a fixed 100-entry ceiling.
The Python search fallback also spools complete matched rows for Run-bound
continuations, including regular expressions. A whole-file memory budget
defaults to 32 MiB; set `HASHI_WORKSPACE_SEARCH_MEMORY_BYTES` to another
nonnegative byte count, or zero to opt out. When a large regex file exceeds the
budget, search reports incomplete coverage and points to `file_search`
recovery. Unreadable or excluded files and excerpt-only matches remain explicit
coverage limitations. Long inventories of skipped paths have a separate
disk-backed continuation; if its storage fails, matching results remain
available and the missing inventory is reported. Workspace inspection searches,
Git status/diff and snapshots exclude the actual private result store and
existing read-protected descendants.
Snapshots also exclude the active tool audit receipt so recording a check
does not itself invalidate the next one; the snapshot reports excluded paths
and preserves Git staged and unstaged state. Git snapshot output is spooled and
hashed in blocks without materializing full diffs or untracked-file lists.
This check does not freeze the filesystem during the full snapshot.
On startup, the owner reconciles abandoned terminal Runs; a Run whose state
cannot be proven is retained until it can be reconciled. A Registry without an
Agent home uses a private temporary store for that Registry's lifetime.
A context without a Run identity cannot use this path. Retained text is read
in pages; legacy screenshot continuations read indexed frames rather than
reloading the full result. Store or integrity failures are reported, not
treated as successful retrieval. Concurrent retention waits for the shared
store lock by default; operators may set `HASHI_TOOL_OUTPUT_LOCK_TIMEOUT_SECONDS`
to a nonnegative total wait budget when needed.

## Browser and desktop deadlines

Standalone browser text, HTML and evaluation navigation defaults to
`domcontentloaded` and accepts `timeout_ms` and `wait_until`. Omitted
`max_length` returns all extracted text for the normal Run-bound result paging;
an explicitly requested `max_length` returns a clearly marked prefix.
The browser extension currently supports explicit `load` readiness only.
Its selected navigation timeout is forwarded through session setup, the client
and native host. A lost response after dispatch is reported as unconfirmed and
is not automatically resent. The legacy extension transport does not yet
guarantee interruption of an action already running in another thread or
extension; multi-step session deadlines and installed-worker behavior need
separate qualification.

Desktop tools accept `timeout_s` for each subprocess. A timeout or cancellation
requests cleanup of the owned process tree and reports the cleanup result.
Typing is not automatically repeated after an error because input may already
have occurred. Actual Linux desktop and other platforms still need validation.

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
Repeat rules do not block or short-circuit execution. Search record-size checks
are advisory observations: a completed shell search can suggest `log_query`
for long records, but a lexical pattern is not an execution denial.

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

## HTTP response and write budgets in the remediation candidate

The ordinary HTTP tool honors its positive requested timeout instead of
clamping it to 60 seconds. The Registry option
http_request.max_response_bytes selects the maximum decoded response bytes
retained in the response body buffer (default 16 MiB). HTTP decompression may
transiently allocate more; the Run-bound output store is not a memory quota.
The tool reads incrementally and reports
the selected limit, response status and whether the full response arrived.
A response within that limit is available in full; when its displayed text
is too long, the existing Run-bound result reader supplies continuation pages.
If the response exceeds the selected limit, the prefix is marked incomplete;
the response may correspond to a request that has already taken effect, and
the tool does not replay it. Cancellation stops waiting and does not retry.

The existing file_write.max_file_size_kb option controls allowed content
size. An oversized write is rejected before touching the destination. File
writes still use the existing direct write path; crash-safe atomic publication
and streaming artifact delivery have not been added by this change. Result
continuations remain process-local and their retained memory is not yet
lifecycle-quota-managed.
