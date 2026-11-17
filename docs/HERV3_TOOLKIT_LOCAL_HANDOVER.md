# HANDOVER — HASHI HERV3 Small-Model Daily/Developer Toolkit

Owner request: implement in an isolated GitHub branch; test only in a local
**non-production** environment; return evidence and a reviewable PR. No merge,
production reboot/restart, production environment or live user Workzone writes.

Repository: `Bazza1982/HASHI`
Branch: `feat/herv3-small-model-toolkit-20261118`
Feature: [HERV3 Small-Model Toolkit](HERV3_SMALL_MODEL_TOOLKIT.md)
Functional owner: PAO Tool Registry; engineering layer: replaceable Functions.
HERV3 only owns the model-visible presentation projection. **No Core changes.**

## 1. Isolation before testing

1. Fetch the new branch and create a separate Git worktree or clone under a
   new development path. Never check it out over a running HASHI installation
   (including `HASHI4`) or its instance state.
2. Do not copy production `agents.json`, `secrets.json`, tokens or live workspaces.
   Set up synthetic Agent identity, independent workzones/ports/bridge_home.
3. Run `git diff main...HEAD --name-only` and
   `python scripts/check_protected_core_changes.py --base main`. Any protected
   Core change is a stop condition; fix in this feature branch without
   requesting a Core migration.
4. Review all new source paths and inspect branch history. Do not run paid
   GitHub Actions, deploy, merge, or touch production.

## 2. Offline focused checks

From the isolated worktree, with its qualified local test interpreter:

~~~powershell
python -m pytest tests/test_herv3_developer_toolkit.py -q
python -m pytest tests/test_her_verification_tools.py tests/test_office_integration.py tests/test_scoped_search.py -q
python scripts/check_protected_core_changes.py --base main
~~~

Then run the repository's documented focused Function/Tool Registry regression
gate and curated Core test gate, only in the test environment. If dependencies
are unavailable, state exactly what was skipped and why; never report a skip
as a pass. Do not trigger/retry GitHub CI to compensate.

Required scenario evidence:

- Schema publication, read-only metadata and explicit allowlist preservation.
- `tool_find({"pack":"office_basic"})` makes *existing* qualified Office
  schemas available on the next model call; never grant an unallowed tool.
- `tool_find({"pack":"verification"})` reveals registry verification tools.
- Shared Registry remains unchanged across concurrent HERV3 Turns. Independent
  tool discovery state does not bleed from one Agent/Turn into another.
- Real `D:/Projects` path scan in a **synthetic non-production workzone**.
  Test SOK alias via Git remote; do not search the full drive by default.
- `code_search` literal/regex/files; result limit, cancellation, slow-query
  per-tool timeout/partial result; the Agent keeps conversing afterward.
- `git_inspect` status/log/diff/remotes with dirty repository; remote URL
  credential scrub; no Git index or branch mutation.
- `text_replace` exact single occurrence, multi-occurrence mismatch, preview,
  atomic update, UTF-8/CJK preservation and out-of-scope rejection.
- `file_manage` mkdir/copy/move, directory recursive mode, overwrite collision,
  cross-Workzone rejection and symlink traversal. No production files.
- `data_inspect` CSV/TSV/JSON/JSONL with CJK and malformed inputs.
- All failures show a truthful typed error/next step, rather than a model
  guess; no Agent time ceiling, extra authorisation layer, or disabled shell.

## 3. Actual HERV3 small/large-model trials

In the isolated test Agent's local backend `tools` options, set:

~~~json
{
  "herv3_tool_presentation": {"mode": "on_demand"}
}
~~~

Do not set it globally. Exercise at least:

1. **Qwen small**: "Find the SOK folder by Git remote, inspect uncommitted
   changes, find where a named function is defined, and suggest a fix."
2. **Gemma4 small**: "Open a sample CSV/JSON, summarise fields, then create a
   backup copy and replace one exact token in a disposable test file."
3. **Office**: discover existing Office tools and read/edit a disposable
   spreadsheet through the qualified Office runtime.
4. **A larger HERV3 model**: same tasks, verify no tool schema/response
   incompatibility.
5. **Default mode without opt-in**: prove previous full catalogue and normal
   HERV3/Provider behaviour are unchanged.
6. **Tool discovery under denied permissions**: never reveal tools outside an
   explicit allowlist, or unavailable Browser/Office executors.
7. **Real UI**: watch Workbench tool activity/output; stop an in-flight search,
   confirm cancellation and Agent recovery without stale spinner.

Measure per-model success, tool calls to first correct answer, time to first
useful result, tool schema count, failure/repair count, and any context budget
difference. Preserve anonymised command/response receipts; no secrets.

## 4. Review and stop conditions

Fix feature regressions on this branch only. Prefer narrow patches; do not
alter HASHI Core, broad resource management, unrelated search-repair branches,
Workbench or Compute. If a necessary fix crosses these boundaries, report it
and request separate approval.

Once tests pass, push any local fixes **to this same feature branch** and
prepare a **draft PR** for Barry's review, or update a draft PR if one already
exists. Do not enable paid CI; provide exact local test commands, results,
platform/version, outstanding skips, and head commit SHA. **Never merge to main
or deploy to a running instance without Barry's explicit instruction.**
