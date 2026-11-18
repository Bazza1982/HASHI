# Authorization, permissions and privacy

## Purpose and when to read
Read for tool denial, permission requests, guarded writes, governance, private authorization, runtime protection and privacy routing.

## Ownership and layer
PAO owns tool capability, permission admission and recovery in Functions. PCM projects trusted authorization facts without granting scope. Frontend Connectors present authenticated decisions; platform isolation remains a separate enforcement boundary.

## Distinctions
Permission levels and model privacy levels are different axes. Identity assurance, relay origin and role names do not grant side effects. An answer to a Run question supplies information, not approval. Unknown commits, private denials and ordinary failures must remain distinct.

Organization and project labels alone do not narrow a Tool grant. An explicitly
selected enterprise workspace root, shell/browser disable or network host list
remains effective alongside the normal resource scope and governance policy.
Request-local metadata can narrow those choices but cannot widen them.

A denied or unavailable managed operation is reported as an error with its
control disposition. An unsupported channel or a disabled capability does not
claim execution success. An unknown prior effect remains a separate recovery
state and must not be treated as proof that retrying is safe.
When a device capability status probe is temporarily unavailable, its existing
granted schema remains discoverable with an unknown-status notice. Execution
still rechecks the owning Worker; a failed pre-dispatch probe is retryable, while
an unconfirmed result after broker dispatch is not automatically retried.

L0 retains ordinary execution; personal L1 approval does not authenticate a governed L2 decision. The opt-in [governed browser approval path](../permissions/GOVERNED_HUMAN_APPROVAL.md) uses a normal frontend URL button, an independently verified human login, exact issued decisions, and the original Run fence. Its invitation grants nothing. Hard L2 Workzone guarantees apply only to qualified HERV3 execution paths; an independent backend or external Compute executor needs separate qualification. Ordinary approval cannot expand the Workzone.

## Authoritative documents
- [PAO tools and permissions](../HASHI_PAO_SYSTEM_DESIGN.md#24-skills-tools-permissions-and-execution).
- [Permission requirements](../permissions/PRD.md).
- [Frontend decision contract](../permissions/FC_CONTRACT.md).
- [Windows L2 contract](../permissions/L2_WINDOWS.md).
- [Live runtime protection](../HASHI_LIVE_RUNTIME_PROTECTION.md).
- [Ledger continuity](../permissions/L2_LEDGER_CONTINUITY_20261105.md) and [trusted offline maintenance](../permissions/LEDGER_MAINTENANCE.md): independent witness, private archives, progress-preserving recovery and idempotent replay.
- [Authority/Run lock ordering](../permissions/L2_POLICY_LOCK_ORDER_20261105.md) and [Windows CI diagnosis](../permissions/PR133_WINDOWS_CI_DIAGNOSIS_20261105.md): durable time, FIFO writer coordination and separate contract watchdogs. Only exact unchanged-state/time reads avoid a redundant flush; new authority or time still requires the atomic dual-database witness commit.
- [Frontend presentation boundary](../permissions/L2_FRONTEND_PRESENTATION_20261105.md) and [formal WAC closeout](../permissions/L2_WAC_CLOSEOUT_20261105.md): distinguish authenticated decisions, pending qualification and historical native receipts.

## Source and verification
Start with `orchestrator/permissions/`, `tools/registry.py` and the scoped capability owner. Use `tests/test_permissions_authority.py`, `tests/test_permissions_guarded_dispatch.py` and `tests/test_permissions_outcome_recovery.py`. Verify stale revisions, identity changes, missing authority, private errors and uncertain publication without retrying effects.

## Adoption
[Implementation status](../permissions/IMPLEMENTATION_STATUS.md) and acceptance evidence describe actual qualification, not blanket readiness. A documented L2 design or isolated dependency check does not prove activation. `documentation_read` is limited to approved Markdown roots and does not widen ordinary filesystem or secret access.
