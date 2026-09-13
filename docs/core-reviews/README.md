# Protected Core review records

This directory stores one immutable, independent review record for each
authorized HASHI Core generation. It does not define which files are Core; the
only source of that scope is
`orchestrator.runtime_contract.CORE_SOURCE_PATHS`.

Core changes are exceptional. Before editing, the current user must explicitly
authorize a **Core major-version migration**. The migration pull request must:

1. raise the `[project]` major version relative to its base and reset minor and
   patch to zero;
2. carry the `core-change-approved` label;
3. add a new JSON record in this directory; and
4. be reviewed by someone other than its implementer.

Generate the digest only after the candidate Core is final:

```bash
python scripts/check_protected_core_changes.py --print-core-digest
```

Create a uniquely named JSON file, for example
`YYYY-MM-DD-v5-reviewer.json`, with this schema:

```json
{
  "schema_version": 1,
  "change_id": "core-v5-migration",
  "authorization_reference": "issue, decision, or dated user authorization",
  "implementer": "implementer identity",
  "reviewer": "independent reviewer identity",
  "reviewed_at": "2026-09-13T23:30:00+10:00",
  "verdict": "approved",
  "product_version": "5.0.0a1",
  "core_digest": "sha256:replace-with-command-output",
  "summary": "What was reviewed, risks found, and why approval was given."
}
```

The branch gate accepts only a newly added record whose product version and
digest match the candidate. A copied historical record, self-review, missing
timezone, non-approved verdict, or stale digest fails closed.
