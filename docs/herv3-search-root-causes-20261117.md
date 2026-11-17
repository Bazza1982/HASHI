# HERV3 search intelligence — root-cause report (2026-11-17)

Branch: `fix/herv3-search-intelligence-20261117` · Base: `origin/main` `0d781c51` (2026-11-17).
Environment: isolated non-production worktree on `D:\`, Windows native. No
production instance was accessed, modified, restarted or deployed.

Each finding is marked **confirmed** (reproduced from source and behaviour) or
**hypothesis** (plausible but not reproduced). Line references are against
`0d781c51` unless noted.

## A1. Missing effective search deadline — CONFIRMED

- `tools/registry.py` `_search_options()` fills `timeout_seconds` only when
  `tool_options.file_search.timeout_seconds` exists or the Smart Registry
  (`smart_registry.foreground_timeout_seconds`, default `None`) is enabled and
  configured. With neither present, no deadline reaches the scanner.
- `tools/file_search.py` `execute_file_search()` sets `config["deadline"]` only
  from that option; without it the worker runs until natural completion
  (bounded only by `max_results` page limit, match/output budget, depth budget
  128). A query matching nothing on a large tree could run for minutes.
- `file_search`, `file_list` and `log_query` all receive the same
  `self._search_options()` dict in `registry._dispatch()` — one shared deadline
  configuration, no per-operation default, and no per-call request field in
  the schemas.
- Configuration merging itself is safe: `_search_options()` copies the dict and
  `execute_file_search()` only reads it; existing backend settings are not
  overwritten.

## A2. Inefficient directory traversal — CONFIRMED

`tools/search_worker.py` `Scanner.batch()` performed, per entry:

- `entry.is_dir()` + `entry.is_file()` (two stats), `signature(path)` (another
  stat) and `path.resolve()` inside `authorized()` for every file and directory;
- for every directory an additional `path.resolve()` plus one `resolve()` per
  ancestor on the stack for the cycle check (O(depth) resolves per directory).

On Windows `Path.resolve()` is comparatively expensive and was the dominant
cost. Measured on the same synthetic tree (301 dirs, 3,000 files, path mode):
1.41 s before, 0.63 s after (~2.2×). Directories can only create cycles through
links, so cycle detection for ordinary directories was unnecessary work.

Additional Windows detail confirmed: `os.DirEntry.stat().st_dev` is `0` while
`Path.stat().st_dev` is the volume serial. Mixing the two in the
cross-filesystem check skips every directory as a foreign filesystem; the
implementation now derives the device id from the same `path.stat` signature
that frames already carry.

Pruning opportunities are limited by correctness: with literal path queries a
non-matching directory can still contain matching descendants, so directory
name pruning is unsafe; exclusions remain the only provably safe prune and are
already reported in coverage metadata.

## A3. Deadline and process-cleanup race — CONFIRMED

`tools/file_search.py` `run_worker()` gave the parent the *same* deadline as
the cooperative worker deadline and, the moment `asyncio.wait` expired,
terminated the process tree. The worker, at the same instant, was serializing
its deadline-limited result (matches + coverage + continuation state). The
parent almost always killed it before the result line arrived, then
synthesized a result with `state: None` — losing the continuation cursor and
any matches found since the last progress event, without reporting the loss.

Reproduced before the fix: a deadline-limited scan reported
`can_continue: false`; after the fix the identical scan reports
`can_continue: true` with a working cursor and
`cleanup.cooperative_deadline: true` (see `tests/test_search_windows.py`).

## A4. Search quality versus mechanical activity — CONFIRMED

`tools/tool_activity.py` and `orchestrator/her_v2/turn_services.py` advanced
progress clocks on enumeration counters (files/directories/characters/bytes)
but not on matches. An Agent could scan unrelated files indefinitely while the
runtime reported observed progress. No mechanism told HERV3 that a search had
hit its budget, was partial, or had found nothing relevant; the existing Smart
Registry repeat advisory only fires on byte-identical repeated results
(`smart_tools.py` `same_result_repeated`), not on low-yield scope expansion.

## A5. Tool wait behaviour — CONFIRMED (behaviour, no defect)

HERV3 executes tools sequentially and awaits each result in
`adapters/her_v2_provider.py` `execute()`; user `/stop` cancels the in-flight
tool and the existing managed cleanup terminates the worker (covered by
`tests/test_search_processes.py`). There is no mechanism for the model to
reconsider mid-tool — that is inherent to the single tool loop, and the correct
boundary is the structured result: partial coverage, stop reason, cursor and
advisory warnings now give the model the facts it needs at that boundary.
Model API latency is a separate path (provider model call) and was not
confused with filesystem search latency.

## SOK / Hashi Compute case study — CONFIRMED

A repository rooted at `D:\Projects\content_generation` with GitHub remote
`Bazza1982/SOK` contains no `SOK`/`Kent`/`Studio` in any scanned filename, so
name-based recursive search cannot identify it; on a synthetic 400-directory
fixture it enumerated 443 directories and 2,400 files and returned zero
matches. Git metadata identifies it immediately. `repo_discover` (new,
read-only) returned the correct repository in ~0.16 s while enumerating only
41 top-level directories and zero files.
