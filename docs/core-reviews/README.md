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
  "reviewed_files_sha256": {
    "pyproject.toml": "sha256:replace-with-reviewed-metadata-digest",
    "constraints/standard-py312.lock": "sha256:replace-with-reviewed-lock-digest"
  },
  "summary": "What was reviewed, risks found, and why approval was given."
}
```

The branch gate also runs for a product major-version change, an edit to the
standard lock, or a change to its declared owner, even when no protected source
changes. Resolve that owner from `[tool.hashi.runtime]` in `pyproject.toml`; the
example path above is not a second declaration of ownership.

The branch gate accepts only a newly added record whose product version,
protected-source digest, and `reviewed_files_sha256` entries for project metadata
and the active standard lock match the candidate. Hash the UTF-8 LF source view
(the index when using `--cached`), and prefix each SHA256 with `sha256:`. Reviewers
may seal additional reviewed files. A copied historical record, self-review,
missing timezone, non-approved verdict, or stale digest fails closed.

Local authorization flags acknowledge an existing user authorization. The
required Architecture CI job receives authorization from the trusted PR label;
flags and a matching independent record cannot replace `core-change-approved`.
An authorized maintainer must apply that label before merge.
