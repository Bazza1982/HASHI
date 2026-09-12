# Nightly repair batch 01 — language preference persistence

Status: focused implementation and local behavioral proof; not a complete W1
closeout or a production-adoption receipt.

## Baseline and scope

Repository: `Bazza1982/HASHI`.
Branch: `repair/nightly-batch-20260911`.
Base: `19e330f985aaf6d5523fd82a3bcc8a4256b536c0`.

Main had advanced from the plan's `fd6167e8` base. Its ConfigAdmin/config_json
repair is retained, not replaced. This batch migrates one further real writer:
Frontend Connector language preferences in the Functions layer. Only
`orchestrator/ui_language.py` changes product behavior. Tests and documentation
are included in the same candidate. Core, protection scripts, manifest, CI,
dependencies, local configuration and running instances are not modified.

## Behavior fixed

- Legacy BOM preferences no longer disappear into an empty display fallback.
- Set/reset cannot turn malformed or incompatible state into a default file.
- Unknown top-level fields and unrelated users' raw settings survive an edit.
- Actor alias set/reset keeps the existing canonical identity behavior.
- Two processes reading the same revision cannot both report a successful
  conflicting publication. A missing-file creation race has the same rule.
- Existing atomic-publication errors propagate without erasing prior bytes;
  post-publication durability errors are not rolled back or silently retried.

## Evidence actually executed

Local environment: CPython 3.13.5 / Linux. It is not HASHI's approved production
runtime. The exact source files needed by this leaf module were materialized
from the base GitHub blobs, with Git blob SHA-1 checks before modification:

| Source | Base blob |
|---|---|
| `orchestrator/ui_language.py` | `22d7086460cd7d47e77ccbd23ba49409d38f616c` |
| `orchestrator/config_json.py` (unchanged) | `65e352f2d724a641048736148eb824d050bbbd91` |
| `orchestrator/process_resources.py` (unchanged) | `31a87c3a022fcbc7dc8fb6407cc309c284f8b264` |

Command:

```text
python -m pytest -q tests/test_ui_language_persistence.py
```

Against base implementation: **21 failed, 4 passed**. The failures include
wrong BOM display state, destructive set/reset, dropped unrelated fields,
and both spawned writers reporting success from the same stale revision.
Against repaired implementation: **25 passed**, no skips or deselections.

The tests use actual temporary files and real process spawning. Failure
injections replace only publication/synchronization boundaries. The race
barrier controls scheduling after real reads; it does not fabricate storage
results. No live credentials, model request, Telegram message or production
state is involved.

This was a focused source-subset run, not a complete repository checkout, the
full offline product suite, the curated Core gate, or native Windows/macOS
qualification. Required integration follow-up in a complete approved runtime:

```text
python scripts/check_protected_core_changes.py --base 19e330f985aaf6d5523fd82a3bcc8a4256b536c0
python -m pytest -q tests/test_config_json.py tests/test_ui_language_persistence.py tests/test_ui_language.py
python -m pytest -q
```

GitHub checks must be evaluated on the actual candidate SHA, not inferred from
this receipt. A skip, pending run, or missing check is not a pass.

## Remaining work / rollback boundary

W1 remains open: this does not implement Memory/Wiki scan database atomicity,
all-writer migration, historical duplicate inventory, or live adoption. Other
repair packages, including Provider recovery and TUI feature work, remain
unchanged. HN-20260911-006/007 remain outside the default batch scope.

Review/pull this repair branch, not main. Do not merge or adopt until the
relevant checks have been reviewed. Source rollback is scoped to this batch;
it must not revert main's earlier ConfigAdmin repair, overwrite saved user
preferences, delete the stable lock file, or restart unrelated Workers. Existing
old generations can still use the old writer and require a separate authorized
adoption plan. No automatic source pull or runtime action is performed here.
