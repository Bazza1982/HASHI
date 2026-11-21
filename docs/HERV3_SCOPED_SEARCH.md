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
# Resolve the validated Windows test interpreter from local configuration.
$scopedSearchTestPython = '<validated Windows CPython 3.14 executable>'
$scopedSearchNativeTemp = Join-Path $env:TEMP 'hashi1-scoped-search-native'
$scopedSearchPolicyTemp = Join-Path $env:TEMP 'hashi1-scoped-search-final-policy'
# Native Windows: owning/platform selection, 25 passed
& $scopedSearchTestPython -B -m pytest -q tests/test_scoped_search.py `
  tests/test_search_activity.py tests/test_search_processes.py `
  --basetemp $scopedSearchNativeTemp
# Final policy amendment: coverage and escaped-output budget, 2 passed
& $scopedSearchTestPython -B -m pytest -q `
  tests/test_scoped_search.py::test_zero_partial_and_exclusions_are_distinct `
  tests/test_scoped_search.py::test_large_escaped_results_page_without_invalid_json_or_lost_hits `
  --basetemp $scopedSearchPolicyTemp
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

**At the initial source closeout, production adoption was not authorized or
performed.** External provider/model behavior and actual user-client rendering
must be distinguished from deterministic tests and the authenticated activity
projection. The existing Strategy Cards were not turned into a mandatory flow.
Optional rg acceleration and persistent indexing are deferred; neither blocks
the base literal/path capability.

## Subsequent approval and HASHI1 live adoption

Approval was subsequently given on 2026-10-04 at 21:40 Sydney time:
"批准继续。批准reboot hashi1。" This authorizes the HASHI1 operational adoption,
not a Core migration or action on another instance. PAO owns the replacement;
Frontend Connector Functions own the observable delivery boundary.

The canonical operator `main.py --bridge-home ... --replace-functions` submitted
one request to the existing broad Function handoff, equivalent to `/reboot max`.
Preflight verified exact configured instance identity, five idle running Agents,
no active background jobs and compatible CPython 3.12.13/Core API 3. The durable
replacement receipt `c7be93c7378645cd8085ea40264db399` reports success for generation
`sha256:3e2b9185395db65b2ced9b7aed9231f53ec72c1e92d48c740273a0b8ff0115c8`.
Its immutable manifest pins source commit
`ade8fc82129b4022372e747530d295a14bbea7d5` and includes the new scanner/activity
modules. This is running-generation evidence, not merely a clean checkout.

The Core PID remains 835 and its full runtime/Core/dependency fingerprint is
unchanged. Shared Functions changed from PID 1157 to 1497623; sunny's Worker
changed from 1297 to 1497780. The exact original five-Agent set is ACTIVE,
accepting and alive on the new generation. All five Telegram connections became
connected, and authenticated health reports ready with no issues. No Remote
lifecycle action, Core cold restart, dependency install or other-instance reboot
was performed.

Three controlled Requests ran in one isolated sunny conversation through the
authenticated Session API, on `deepseek-api/deepseek-flash/high` with actual
`her-v3` delivery:

- Search/read: sunny selected `file_search`, searched only its own home and then
  used `file_read` to return the fixture marker. Audit reports one match, 291
  directories/3,006 files enumerated, zero file-body characters read, completed
  declared coverage and reaped worker/group. The model selected expanded/hidden
  path discovery within that exact root; it did not scan the machine or peers.
- Quiet foreground work: while commentary was off and verbose on, the live
  authenticated activity interface exposed the running operation before the
  ToolResult. The 30.072-second snapshot was a visible update; liveness was alive,
  scanning progress unknown and last actual work/output remained null. The
  40-second fixture finished normally with zero observer failures and a reaped
  process group. A command pointing into the managed `.venv` was first refused
  by existing live-runtime write protection; the allowed `python3` command ran
  the same fixture. Protection was not disabled or relaxed.
- Toggle/cancel: verbose off hid the active snapshot; enabling it exposed the
  current five-second snapshot without restarting the same PID/creation time.
  The owner-scoped cancellation endpoint stopped only this probe. The Run became
  `stopped`; audit confirms SIGTERM/terminated cleanup, `process_reaped=true`,
  `group_alive=false` and no errors, and the probe PID disappeared. Cleanup was
  observed within a six-second polling window; no precise latency claim is made.

The three request IDs end in `0001`, `0002`, `0003` under
`req-sunny-2026-10-04_214410`. Temporary verbose/commentary changes were restored
to their original true/true values, verified from live presentation metadata.
Final health remained ready and every Agent idle. Bounded local evidence and
fixture artifacts are retained in the ignored acceptance/cache directories.

These are live Worker, control and authenticated activity-interface observations.
They do not prove an external client's screen rendering: the connected browser
was leased by another task and no control takeover was attempted. Telegram
connection/delivery and a human observing a progress message are separate facts;
human display confirmation remains unverified. This was focused acceptance,
not the separately governed full essential-live-test manifest.


## 2026-11-17 — recoverable execution budgets, cooperative deadlines and repository discovery

Follow-up engineering on `fix/herv3-search-intelligence-20261117` (PR pending;
not merged, not deployed). Motivation: an Agent on DeepSeek V4 Pro spent
excessive time locating local projects (SOK under `content_generation`, Hashi
Compute) by repeatedly exploring `D:\Projects` with ever-broader `file_search`
queries. Root causes confirmed in `tools/` and `orchestrator/her_v2/`:

1. **No effective default deadline.** `registry._search_options` filled
   `timeout_seconds` only from explicit `tool_options.file_search` or a
   configured Smart Registry foreground fuse. With neither present, a scan ran
   until natural completion (page/output budget only). `file_search`,
   `file_list` and `log_query` unintentionally shared that single configuration.
2. **Parent/worker deadline race.** `file_search.run_worker` killed the worker
   the instant the deadline fired — the same instant the worker was trying to
   serialize its deadline-limited result. The valid continuation state was
   almost always lost; the synthesized result reported `can_continue=False`
   and lost matches found since the last progress event.
3. **Inefficient traversal on Windows.** `Path.resolve()` ran for every entry
   and per directory ancestor (cycle check), and multiple `stat` calls
   (`is_dir`, `is_file`, `signature`) per entry. A subtle Windows detail:
   `DirEntry.stat().st_dev` is 0 while `Path.stat().st_dev` is the volume
   serial, which must never be mixed in the cross-filesystem check.
4. **Repository discovery by name only.** Searching folder/file names for
   `SOK`, `Kent` or `Studio` cannot identify a checkout whose directory is
   `content_generation` and whose GitHub remote is `Bazza1982/SOK`.
5. **Mechanical progress signals.** Rising enumeration counters counted as
   activity progress even when no matches were found; HERV3 had no
   deadline/partial-result guidance in `v3_prompt`.

Changes (all in Functions territory; protected Core untouched):

- `tools/file_search.py`: `DEFAULT_SEARCH_BUDGET_SECONDS` (45/15/45/45/30),
  `resolve_search_budget` (per-call request > configured > default, optional
  `timeout_max_seconds` cap), parent cleanup grace window before emergency
  process-tree termination, explicit `continuation_lost` reporting and
  actionable deadline warnings. Guidance uses the actual allocated continuation:
  it offers resumption only when the scan state is retained. If the scan state
  was lost, a cursor that only pages buffered matches is described as such,
  and the remaining coverage requires a fresh scan. Regex content searches
  report incomplete coverage for unbounded repeated literal alternatives where
  one alternative prefixes another; disjoint alternatives and finite repeats
  remain available. Other costly expressions may still reach the deadline.
  Backward compatible: existing configured timeouts keep their exact semantics.
- `tools/search_worker.py`: one `DirEntry.stat` per entry, physical-prefix
  authorization for ordinary descendants (`resolve` only at roots/links and
  cursor frame validation), cycle checks for linked directories against all
  ancestor frames, path-mode batches continue past files
  (≤64 entries per batch), deadline notice kept out of continuation state, and
  a guaranteed minimum of one batch per invocation so a deadline can never
  produce a zero-progress continuation loop.
- `tools/repo_discover.py` (new): read-only Git-metadata repository discovery;
  no daemon, index, network access or new authorization. Finds
  `content_generation` as SOK in ~0.16 s on a synthetic 400-directory fixture
  without enumerating any repository contents, where recursive name search
  enumerated 443 directories / 2,400 files and found nothing.
- `tools/registry.py`/`schemas.py`/`smart_tools.py`/`builtins.py`: wiring,
  schemas and per-call `timeout_seconds` arguments for all four search tools;
  `repo_discover` added to the read-only catalog.
- `tools/tool_activity.py` + `orchestrator/her_v2/turn_services.py`: match
  discovery (`last_match_at`, `match_progress`) distinguished from mechanical
  enumeration, exposed to activity observers as advisory counts only.
- `orchestrator/her_v2/v3_prompt.py`: concise search-intelligence guidance —
  known locations first, targeted discovery before wide recursion, Git-based
  repository identification, partial-result/cursor decision ownership, strategy
  change on low-yield repetition, exhaustive search when genuinely required.
  No fixed tool sequence, search count, planner or blocker was added.

Validation (synthetic fixtures, isolated D-drive environment, Windows native):
see the PR description for commands and counts. No live-model claim is made
where only deterministic fixtures were used; no production instance was
accessed, modified, restarted or deployed.


## 2026-11-18 — PR #170 independent-review repairs

PAO Tools owns these Functions-layer repairs. Every scanner directory frame now
retains its resolved physical prefix, including ordinary ancestors. A plain child
inherits that prefix; only a link/junction needs target resolution. Descendant
path and content checks use the physical path against the same selected/read
roots and protected-read paths. Cursor frames retain and validate that mapping;
cycles back to ordinary roots/ancestors terminate without per-file resolution.

Repository discovery applies the existing read resolver and projected protected
read paths before following or reading .git, gitdir, commondir, config,
config.worktree and HEAD. Denied or unreadable metadata suppresses that candidate
and yields partial coverage with a candidate-level error code; it never adds
metadata directories to the read scope. Missing optional configs remain optional.
The individual worktree HEAD precedes the common HEAD, with fallback only for
missing metadata. Remote URL userinfo, query strings and fragments are omitted
before matching or returning results. Config includes and network operations are
not followed.

The frozen L0 observation is unchanged. A separate reviewed delta pins the two
authorised current hashes, and the test reverses only the exact file_list budget
schema/description and HERV3 guidance additions to verify the historical hashes.
Every other permission, tool, audit and runtime assertion remains mandatory.
The added search tests belong to the existing Permission focused inventory;
pyproject testpaths is restored, avoiding the unnecessary full-CI trigger.

Regression fixtures cover allowed directory symlinks and Windows junctions,
protected descendants never opened, path/content pagination and link cycles,
ordinary-file resolution cost, metadata pointer/file scope and protection,
access-denied reporting, credential-bearing remotes, and a real main/topic Git
worktree. The same fixtures fail on reviewed HEAD ce8b25f9 and pass after repair.
Validation is local on isolated native Windows CPython 3.12; Unix-specific
acceptance remains separate. No hosted suite, production adoption, merge or
deployment is part of this review repair. Repository discovery still has no
continuation cursor; its existing partial-result and narrowing guidance remains.

## PR #186 candidate — bounded regex content search

PAO Tools supports regex content matching within logical lines. LF and CRLF
terminators are excluded from the matched line, while offsets count the actual
decoded characters, including both CRLF characters. Matches are reported in
file order; when several expressions match at the same position, expression
order breaks the tie. Pagination retains the whole bounded line and each
expression's progress, so anchors and lookbehind keep their original context.

A record exceeding 1,000,000 characters is skipped in fixed-size chunks; it is
never collected in full. The scan continues, reporting incomplete coverage and
the reason. Once the input is exhausted no cursor remains, even if coverage is
incomplete. Nested quantified expressions rejected by the scanner likewise
report unsupported coverage. Regex matching does not span logical lines.
Cancellation and per-call budgets remain in force while skipping. This describes
the PR candidate; it does not assert deployment or live adoption.
