# Batch 04 — API Gateway configuration persistence

Base: `3450b5b9fe534a688e748701595d7450c3d558ce`.
Branch: `repair/nightly-batch-20260911`; existing draft PR #16.
Scope: bounded W1 / HN-20260911-002 continuation. Owner: PAO / shared Functions.

The user deferred batch 03 (private Memory/Wiki scanner) to local work and
requested the next repository-owned repair on this branch. Batch 03 is not
complete. No private scanner, database, memory or configuration is published.
This batch is not all-writer closeout, production adoption or a merge approval.

## Defect and changes

The Gateway used a PID-named temporary file without the shared revision/lock
contract. Its save operation consumed the effective read view: malformed/BOM
input could become defaults, unknown fields disappeared, and concurrent saves
could both report success while overwriting each other. Legacy initialization
could overwrite a newly created canonical file. A rejected save could already
have published a separate migration. ServiceManager also changed its enabled
flag before attempting persistence.

- `orchestrator/api_gateway_config.py` now uses unchanged `config_json` for
  real document reads, strict saves and create-only legacy seeding. The public
  four-field view, model resolver, configured instance opt-ins and file paths
  remain. Unowned fields and retained legacy bytes are preserved.
- `orchestrator/service_manager.py` only reorders three flag assignments after
  successful persistence. No new start, stop, retry or lifecycle mechanism.
- `tests/test_api_gateway_config.py` exercises actual files, spawned writers,
  publication failures, legacy migration, instance model resolution and the
  immediate ServiceManager consumer. Existing tests are not weakened or removed.
- The owning persistence contract records behavior and operator/Agent FYI.

A save conflict raises without retry. A concurrent legacy initializer simply
leaves the canonical winner alone. Publication-before-directory-sync failure
remains a committed error, not a reason to restore old bytes or start/stop a
service. File and external service state are not an atomic distributed commit.

## Source provenance

Exact source bytes were materialized from GitHub and Git blob hashes checked
before editing. This is a source subset, not a complete Git checkout.

| File | Base blob SHA |
|---|---|
| `orchestrator/api_gateway_config.py` | `06aa7c703ed285404528533495df07ae8be84ac7` |
| `orchestrator/service_manager.py` | `1831be660ebb4786f7227afba4244c1cd2474deb` |
| `orchestrator/config_json.py` (unchanged) | `65e352f2d724a641048736148eb824d050bbbd91` |
| `orchestrator/process_resources.py` (unchanged) | `31a87c3a022fcbc7dc8fb6407cc309c284f8b264` |
| `orchestrator/model_catalog.py` (unchanged) | `09c32983c205f813796be038559b27ca71e60cb8` |
| `orchestrator/flexible_backend_registry.py` (unchanged) | `7573c99faa88605baf1ade980306071ebb57fd72` |
| `orchestrator/runtime_effort_options.py` (unchanged) | `f4682ce541575e4de42f65e5764d7abd5c31db04` |
| `docs/HASHI_CONFIGURATION_PERSISTENCE.md` | `a5c63019ccffc641b53b56ca3271e71ebdc2d8d8` |

## Local verification actually run

Linux / CPython 3.13.5 / pytest 9.0.2, not the approved 3.12.13 environment.

```text
python -m pytest -q tests/test_api_gateway_config.py -k 'not service'
```

All 25 persistence/registry cases passed, 6 service cases explicitly deselected.
The initial pre-fix 25-case run had 23 failures and 2 compatibility passes.
These tests import the actual Gateway owner, config primitive and model/effort
registry closure: no model catalogue or persistence function is stubbed.

A separate local-only import-isolating plugin made the full 31-case module
runnable. It provides fail-fast placeholders for six unused ServiceManager
imports (UI language, directory, background jobs, scheduler, service endpoint
error, delivery watcher). It does not replace the ServiceManager class or its
methods, Gateway logic, file reads/writes, model rules, locks or revisions.
Actual service start/stop boundaries are observed by offline test doubles.
The plugin is outside the repository and is not included in this commit.

```text
PYTHONPATH=..:. python -m pytest -p local_consumer_imports -q tests/test_api_gateway_config.py
```

Exact baseline product files: **26 failed, 5 passed**.
Candidate: **31 passed**, no skips or deselections. Final repeat: 31 passed.
The five compatibility-positive cases were also proven sensitive to temporary
local mutations (wrong public shape, ignored instance opt-ins, omitted service
persistence): **5 failed, 26 deselected**. No mutations are in the candidate.
Python compilation of both product files and the new test module passed.

This evidence is not full-import integration of ServiceManager, native Windows
qualification, a real Gateway request or live adoption. No real instance,
service, Provider, credentials or user configuration was used.

## Integration and publication gates

The actual GitHub diff and unchanged architecture workflow must be reviewed on
the submitted candidate. That workflow includes ServiceManager-related and Core
boundary tests, but does NOT explicitly select this new focused module or the
Gateway command module. Its result must not be presented as their result.
Run in a complete approved environment before promotion:

```text
python scripts/check_protected_core_changes.py --base 3450b5b9fe534a688e748701595d7450c3d558ce
python -m pytest -q tests/test_api_gateway_config.py tests/test_api_gateway_command.py
python -m pytest -q
```

The last command is the curated Core gate, not every offline test. The known
FYI truncation failure is not repaired or hidden here; current CI status and
any new failures are recorded separately in the PR conversation.

Core, manifest, protection script, workflow, dependency generation, model list,
ports and instance data are outside the diff. No authorization bypass, force
push, main update or live restart. Keep the PR draft and unmerged.

## Local acceptance and rollback

The user owns native-platform testing and adaptation. A source pull does not
change an already running immutable Function generation. The shared Gateway
owner may require separately scoped shared adoption; do not widen an Agent
reboot or affect unrelated Agents. Old writer generations and arbitrary editors
do not participate in the new locking protocol.

Rollback only this source batch; retain actual saved configuration, backups,
legacy bytes and stable lock files. Never undo preceding batch-01/02 repairs
as a shortcut. W1 still has other writers and local scanner work outstanding;
Provider/HER, TUI routing, model metadata and media packages remain open.
HN-20260911-006/007 remain outside the default scope.
