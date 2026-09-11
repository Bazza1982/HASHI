# Nightly repair batch 02: group configuration transactions

Authorization: user requested the next batch after batch 01. Scope remains the
original approved repair set; HN-20260911-006/007 are excluded. This is a bounded
W1 / HN-20260911-002 continuation, not W1 closeout or release approval.

Base: `a62d08cb75ce27a9bd33d25401e2fddbf348b2c5` on
`repair/nightly-batch-20260911`, existing draft PR #16.
Functional owner: PAO. Engineering layer: Functions.

## Defect and repair

Group mutation previously changed `AgentDirectory._groups` before persistence,
then replaced the entire on-disk group map from that potentially stale cache.
It also wrote `agents.json` directly, outside the existing config file lock and
revision contract. This could lose other group updates, undo membership cleanup,
and leave a changed cached view after failed persistence. A shallow group view
also exposed nested mutable state without a successful save.

One private group-edit context now reads the actual document before business
decisions and publishes the revision-bearing candidate before updating its
view. It reuses `config_json` unchanged; it adds no storage service, format,
external dependency, model rule, or retry loop. The five public operations and
their messages retain their contracts. Group views become detached snapshots.
See [configuration persistence](../HASHI_CONFIGURATION_PERSISTENCE.md).

Files changed in this batch:

- `orchestrator/agent_directory.py`
- `tests/test_runtime_groups.py`
- `docs/HASHI_CONFIGURATION_PERSISTENCE.md`
- this receipt

## Local verification actually performed

This environment provides Linux CPython **3.13.5**, not HASHI's approved
3.12.13 runtime. This verification uses a source subset, not a complete
repository checkout. The complete AgentDirectory source and its real config primitive, process locks and protocol
module were materialized from GitHub and checked against their Git blob hashes.
The original guard script and Core manifest source were also hash-verified and
left unchanged in a temporary subset Git repository.

A local-only external runner isolates the unused pathing import (its two
helpers raise if executed) and the frontend module import. It does not replace
group methods, file IO, revision checks, OS locks or spawned processes. The four
frontend cases are explicitly deselected locally. Neither this runner nor the
import isolation is included in the repository change or GitHub CI.

Exact local selection, run before and after the product fix:

```text
python /mnt/data/hashi-batch02-harness/run_focused.py -q tests/test_runtime_groups.py -k "not test_group_list_view_handles_empty_directory and not test_group_command_creation_stays_in_group_module and not test_group_command_rejects_missing_directory and not test_group_command_does_not_report_success_when_publication_fails" --tb=short
```

- Original AgentDirectory: **28 failed, 4 deselected**, 1.42 seconds (resumption verification).
- Candidate AgentDirectory: **28 passed, 4 deselected**, 1.26 seconds (resumption verification).
- Real temporary files and two spawned processes. Both old writers report
  success and lose a member; the repaired pair yields one saved result and one
  revision conflict. A separate deliberate fresh operation retains both edits.
- Scenarios include all five mutations/no-ops, preservation of newer state,
  BOM/CRLF reads, missing/corrupt configuration, private-candidate failure,
  committed durability failure, detached views, and no resurrection of already
  persisted membership cleanup. No historical data was read or cleaned.
- The unchanged protection script passes on the materialized subset without
  `--authorized`; full-repository checks remain the actual GitHub CI's job.

The existing command creation test now uses a real AgentDirectory and actual
saved bytes; an additional command failure case requires no success reply when
file synchronization fails. These four frontend cases require full imports.
All 32 cases live in the existing group module already selected by architecture
CI. No test starts another test runner or silently expands collection.

## Gates and remaining work

The user explicitly approved submitting batch 02 and will perform local testing
and adaptation. This source candidate remains on the existing repair branch
and draft PR #16; it is not approval to merge main or operate an instance.
Product and test blobs are byte-identical to the reviewed candidate patch:
`7ef96ee464176c2d8ec9de08463de13ae28fb2fa` and
`aed77478149b5bf98c977937f4813bc42e293217`, respectively.
Only this receipt's publication status is updated from that patch.

An earlier documentation upload was blocked during candidate preparation, at
which point no commit/ref was created. Following renewed explicit submission
approval, the normal GitHub blob action accepted the unchanged documentation.
See PR #16 for the published commit and actual CI result; the historical local
results above are not a claim that a new CI run has passed.
Baseline/batch-01 architecture CI has a confirmed pre-existing FYI truncation
failure (390 passed, 1 failed). The FYI loader, reference, assertion and budget
are unchanged here. Do not skip that failure or widen budgets to obtain green.

The approved-runtime group module, curated Core gate, independent review,
native Windows/macOS checks and live Function adoption are separate evidence.
The prior locale persistence suite is not automatically added by this batch.
No Core, workflow, dependency, main ref, instance configuration or running
process is changed. No restart, scan rerun, Sunny replay or state cleanup occurs.

Keep the PR draft and do not merge automatically. Memory/Wiki scan preflight,
database atomicity/identity, remaining writers and later work packages remain
open. Existing/old-generation writers and arbitrary external editors are not
made transactional by this change.

## Rollback boundary

Revert only this batch's source commit when appropriate; do not restore an old
`agents.json` or delete lock files. Such a source revert reintroduces the old
writer risks and does not revert a committed group change. Production adoption
or rollback still needs separately scoped operational authorization.
