# HERV3 scoped search — 2026-10-04

## Decision, authority and scope

The user authorized implementation in HASHI1 only, on a local branch, with no
push, merge, production restart or Core migration. Work started from clean
`0bcbff4b` on `feat/herv3-scoped-search`, newer than the design review baseline
`6a4b29b8`. Existing runtime protection, Run-frozen Workzones, platform shell
resolution and process-tree/group cleanup were retained.

Success means choosing relevant locations, truthful coverage and observable,
cancellable execution. Shell is a first-class tool. No extra planning/review
model, mandatory search sequence, broad-root denial, index or daemon is added.

## Functional ownership

| Owner | Functions files | Behavior |
| --- | --- | --- |
| PAO | `workzone`, `runtime_workzone`, `flexible_backend_manager` | Derive exact Run scope, own home read grant, cwd and revision |
| PAO Tools | `registry`, `schemas`, `file_search`, `search_worker`, `literal_search`, `tool_activity`, Gateway | Admission, bounded execution, continuation, cancellation and observations |
| PCM | Workzone projection; PCM design | Paths/labels remain data; no second profile writer |
| HERV3 | `v3_prompt`, provider wrapper, `turn_services` | Single main loop, frozen scope forwarding, AC factual progress |
| Frontend Connector | `request_activity`, `her_message_router`, runtime locale catalogs | Technical/verbose projection, live snapshots and terminal/generation fencing |

Protected Core is unchanged. External client repositories are not changed.

## Delivered contract

`file_search` defaults to path literal discovery. Content mode scans ordinary
UTF-8 text in bounded chunks, with replacement/encoding warnings. Path glob uses
root-relative `/`, per-segment `*`, and recursive `**`. Containers, media and
binary bodies are excluded explicitly; path discovery can find them. Regex is
honestly unavailable in this first version, with literal/path and Shell retained.

Default roots are enabled exact Run Workzones plus own Agent home, never the
widest access root or common parent. Explicit roots replace defaults.
Tool-explicit provenance denotes the caller's selection, not proof that a user
requested or authorized it. A PAO
scope carries owner/Agent identity, home, cwd and revision; the HERV3 wrapper
freezes that projection for subsequent tools. Gateway schema 6 carries both exact
roots and scope, accepting historical schemas 3–5. Request allowlists, delegated
read-only classification, enterprise and live-runtime denial remain in force.
Home read access does not extend file-write, patch or Shell permissions.

The scanner is a short-lived foreground child, using argv/data and the existing
process cleanup owner. Lazy directory stacks and bounded text batches alternate
between roots. Directory links/junctions and mount traversal are off by default;
following a link still rechecks selected and authorized roots and detects cycles.
Project exclusions are defined once and reported, including directory names,
internal ledger/checkpoint filenames and excluded content suffixes. Expanded
profile and hidden inclusion change discovery preferences, never authority.
The project profile prunes nested `workspaces` trees, so an instance checkout
does not implicitly search every colocated Agent. The exact own-home root is
scheduled independently. An explicitly selected workspace root remains usable
under its existing read permissions; this exclusion is a preference, not a grant.

Results retain the existing five-field Smart envelope and report selected roots,
revision, matches, exclusions, counters, coverage, stop reason and continuation.
Complete empty searches succeed; incomplete zero results are partial. Structured
payloads are budgeted before transport instead of slicing JSON. Continuations
are opaque references to bounded Function-local memory (64 entries, 8 MiB total,
15-minute TTL, 3 MiB per entry). They bind query, roots, policy, Agent, owner, Run,
revision and Function generation, are consumed on use and never deserialize model
state. File/directory signature changes invalidate a cursor without a rescan.
Function replacement clears this derived cache. No scan survives a page return.

Recursive `file_list` uses the same foreground scanner and truthful pagination;
the initial page defaults to 50, subject to serialized budget. Names/types and
coverage remain available; callers can request further pages. `log_query` retains
its literal one-file contract and bounded excerpts, now with actual progress,
cooperative managed-process cancellation and continuation. The underlying literal
scanner is shared, not copied. Legacy audit and Smart ledgers use PAO Agent home
when available, with their file established before a directory snapshot so the
tool's own receipt cannot invalidate its next page. This is runtime audit writing,
not a new user-tool write grant.
The existing audit owner also records the verified selected scope before a typed
scan starts, then coverage/cleanup at the terminal boundary. Start receipts are
explicitly marked `started`, never completed observations. HERV3 preserves native
search envelopes and attaches evidence references inside their data; it does not
append a plaintext receipt after JSON. Repeat advisories compare search evidence
while retaining the original full audit hash, so fresh operation IDs neither
hide repeated empty searches nor confuse cursor pages with semantic loops.

Shell drains stdout and stderr concurrently, retaining bounded head/tail data and
real byte totals. There is no automatic background conversion; `/bg` authorization
is unchanged. Deadlines derive from effective tool/instance configuration; a
search-specific override is optional, and no short default is introduced. Wide
scope is an annotation, including a few reliably parsed literal native forms;
dynamic Shell scope remains unknown. The prior unsafe long-record admission is
retained separately.

Execution snapshots use technical delivery, independently of commentary. Defaults
are 5-second internal snapshots, first long-operation visibility after 30 seconds
and 150-second visible updates. Quiet updates coalesce instead of creating chat
messages. An authenticated activity poll after verbose is enabled projects the
latest active snapshot. Only whitelisted fields survive projection; duplicate,
terminal and stale-generation updates are fenced. Counters measure actual work;
Shell silence means unknown scanning progress. AC receives bounded activity facts
and root count, not queries/hits/path sets; heartbeat cannot advance its progress
clock, and it gains no process-control authority. MCP clients requesting a
`progressToken` receive bounded progress notifications while a call runs, and
`notifications/cancelled` remains responsive to managed foreground cleanup.

## Configuration and rollback

Existing wildcard tool configurations include the new read-only search. Explicit
allowed-tool configurations must opt into `file_search`; request allowlists still
narrow them. No Agent configuration was replaced or changed to wildcard.

`tools.file_search.enabled=false` removes discovery from the catalog and revokes
the additional home read projection; Workzone projection states that scoped search
is disabled. Scope identity/revision facts remain available; old tools continue
with their original roots and their shared foreground scanner remains enabled. Default profile, page
and observer cadence use normal tool options; no `/searchmode` command is added.
For source rollback, revert the scope/capability/prompt changes together and build
a matching Function artifact. Do not overwrite Agent/Workzone configuration.
Output buffering and process cleanup must remain bounded under rollback.

## Validation and adoption evidence

Initial red evidence: three tests failed because the tool was unregistered, home
reads were denied with main Workzone active, and waiting Shell calls produced no
runtime observations. The same scenarios pass after implementation. Additional
tests exposed and fixed audit-induced stale cursors, lost live-toggle metadata,
terminal event fencing, and native Windows CRLF offset assumptions.
Focused red/green probes also found ANSI-encoded worker pipes breaking Chinese
paths, a native search envelope being corrupted by the HERV3 evidence footer,
an instance checkout visiting colocated workspaces, and the rollback flag
accidentally disabling old listing/log tools. A final coverage probe exposed
unreported default filename exclusions; an assertion failed on that omission and
passes with the complete effective policy. These defects have behavior checks.

Focused checks cover real search/read isolation, exact roots, Gateway round trips,
long-record paging without duplicate/lost hits, stale/cross-Run cursors, hidden and
dependency inclusion, container exclusions, Unicode paths, links, partial roots,
verbose/commentary orthogonality and actual progress clocks. Real foreground
probes exercise cancellation, deadline reporting, simultaneous stdout/stderr
draining and bounded parent allocations. Existing Bash process-tree tests verify
descendant cleanup and isolation from unrelated groups. Final commands/counts and
the required curated Core gate are recorded in the implementation handoff.

WSL CPython 3.12.13: the owning and direct-consumer selection passed 196 tests.
Native Windows CPython 3.14: 25 scoped-search/activity/process tests passed,
including a real MCP progress/cancel round trip. Windows Unicode coverage also
forces an inherited ANSI pipe encoding instead of relying on UTF-8 defaults.
This is platform evidence, not qualification of Python 3.14 as a HASHI runtime.
The curated gate initially passed 739 checks and refused three generation
qualifications because the Functions source was not yet committed. After the
coherent source commit, all 742 checks passed in 229.42 seconds; the commit
requirement was retained. The final exclusion-policy amendment passed 56 owning
and direct-consumer checks, plus two native Windows coverage/budget checks.
These runs had no failed or skipped tests.

Reproducible commands (run in the HASHI1 checkout):

```bash
# WSL CPython 3.12.13: final policy and direct consumers, 56 passed
.venv/bin/python -B -m pytest -q tests/test_scoped_search.py \
  tests/test_smart_tool_registry.py tests/test_tool_output_limits.py \
  tests/test_tool_gateway_mcp.py
# Curated Core gate from committed Functions source, 742 passed
.venv/bin/python -B -m pytest -q
```

```powershell
# Native Windows: owning/platform selection, 25 passed
& 'C:\Python314\python.exe' -B -m pytest -q tests/test_scoped_search.py `
  tests/test_search_activity.py tests/test_search_processes.py `
  --basetemp 'C:\Users\thene\AppData\Local\Temp\hashi1-scoped-search-native'
# Final policy amendment: coverage and escaped-output budget, 2 passed
& 'C:\Python314\python.exe' -B -m pytest -q `
  tests/test_scoped_search.py::test_zero_partial_and_exclusions_are_distinct `
  tests/test_scoped_search.py::test_large_escaped_results_page_without_invalid_json_or_lost_hits `
  --basetemp 'C:\Users\thene\AppData\Local\Temp\hashi1-scoped-search-final-policy'
```

The protected Core guard and `git diff --check` passed. Offline generation probes
prepare temporary instances only; they do not adopt artifacts into production.

Manual first-action model probes used the actual v3 prompt/compiler and tool
schemas, synthetic Workzone/home context, Strategy Cards off, and
`deepseek-api/deepseek-flash/high` plus `deepseek-api/deepseek-v4-pro/high`.
Six requests per model covered recall, repository symbols, a known JSONL ID,
explicit whole-drive intent, Git history and hidden/generated files. All 12
started within task-related scope or the user's explicit broader intent; recall
used memory search and known logs used `log_query`. Shell was used directly for
native/Git operations. The daily model sometimes added redundant directory
inspection. Only the first Provider action was observed: no probe executed its
Shell, searched a real disk, read live Agent memory or proved a full task outcome.

Four additional Provider probes interpreted real temporary-fixture tool results:
a completed empty search and a deadline-limited empty search for each model.
Both models qualified the completed result by its selected scope/policy and
recognized that the partial result cannot establish absence. The daily model was
verbose and overcautious about skipped entries; this observation motivated the
explicit filename-exclusion reporting above. These probes are bounded behavioral
evidence, not a guarantee across all models or multi-turn tasks.

**Source and offline verification are not production adoption.** No new Function
artifact was published to HASHI1, no live Worker was replaced and no production
restart was used. External provider/model behavior and actual user-client rendering
must be distinguished from deterministic tests and the authenticated activity
projection. The existing Strategy Cards were not turned into a mandatory flow.
Optional rg acceleration and persistent indexing are deferred; neither blocks
the base literal/path capability.
