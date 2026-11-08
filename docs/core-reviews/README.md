# Protected Core review records

This directory stores immutable approval records for authorized HASHI Core generations.
The authoritative protected source scope is `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.

A Core major-version migration requires:
1. explicit owner authorization for the major-version change;
2. an increased `[project]` major version with minor and patch reset to zero;
3. the `core-change-approved` PR label;
4. a newly added, truthful JSON review/approval record matching the exact Core digest;
5. the full applicable CI and runtime contract checks.

## Two valid approval modes

**`independent_human`** (default, original policy): a real reviewer other than
the implementer reviews the Core and signs the record. The `reviewer` must
differ from `implementer`.

**`owner_approved`** (solo-owner projects): the actual human project owner
explicitly authorizes the final Core migration and accepts the recorded
technical review evidence. A second human is **not** required. The record
must contain `owner`, `owner_decision: "approved"`,
`owner_decision_reference`, and `technical_review_reference`. The
`reviewer` field identifies the technical reviewer accurately (including
an AI reviewer, when applicable); it must never imply an AI is a human.
An Agent may assemble and submit the record **only after the owner has
explicitly authorized the action**; an Agent cannot independently grant owner
approval. The source check validates record structure and digest, not the
authenticity of a referenced external conversation. That remains an auditable
owner responsibility.

Generate the candidate digest after the Core source is final:

```bash
python scripts/check_protected_core_changes.py --print-core-digest
```

Example for an explicitly approved solo-owner project:

```json
{
  "schema_version": 1,
  "approval_mode": "owner_approved",
  "change_id": "core-v5-migration",
  "authorization_reference": "Dated owner authorization for major-version migration",
  "implementer": "Actual implementation identity",
  "reviewer": "Actual technical reviewer (AI or human, identified accurately)",
  "owner": "Actual human project owner",
  "owner_decision": "approved",
  "owner_decision_reference": "Dated explicit owner decision to approve and merge",
  "technical_review_reference": "Link to technical review and test evidence",
  "reviewed_at": "2026-11-07T19:00:00+11:00",
  "verdict": "approved",
  "product_version": "5.0.0a1",
  "core_digest": "sha256:replace-with-command-output",
  "summary": "Actual review scope, evidence, remaining risks and owner decision."
}
```

The branch gate requires a newly added JSON record with a matching product
version and digest. Historical copied records, missing timezone, unsupported
approval mode, invalid owner decision, false identities and stale digests must
not be used. Passing the record gate does **not** waive downstream CI tests or
authorize production deployment.

## Dependency revisions and Core contract metadata

Independent CR/FR/helper dependency, interpreter and build revisions use their
own runtime qualification and lifecycle. They do not require a Core major,
approval label or Core review record merely because a runtime identity changes.

A change to the versioned Core API/Function API/Worker/schema declarations is
an actual Core contract change even if protected source bytes stay unchanged.
It retains the existing Core release/approval requirements. Its newly added
review record also supplies reviewed_files_sha256 for pyproject.toml, hashing
the exact UTF-8 LF candidate text (the index for --cached). Missing/stale seals
fail closed. Independent role locks are not added to this Core approval scope.

The three 2026-11-05 PR #137 records describe the earlier coupled-runtime
candidate and remain immutable historical evidence. They are not approval of
the later integrated tree. Current PR validation/merge status and runtime
adoption evidence remain separate from every historical review record.
