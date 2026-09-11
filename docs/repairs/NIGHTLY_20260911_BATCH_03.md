# Nightly repair batch 03: local Memory/Wiki scan atomicity

Date: 2026-09-12 (AEST). Work package: W1 / HN-20260911-002.
Repository branch: `repair/nightly-batch-20260911`.
Main baseline: `19e330f985aaf6d5523fd82a3bcc8a4256b536c0`.

## Authority and scope

This batch repairs the machine-local, Git-ignored Memory consolidation scanner
after the repository-owned persistence batches were pulled and validated. The
scanner remains ignored and is not made public by this receipt. The receipt is
tracked so the local adoption boundary, tests and remaining work are visible.

Functional owner: the existing Memory consolidation job. Engineering layer:
Functions. Protected Core, Memory schemas, Wiki curation, embedding models,
retention rules and production data are unchanged. No production scan, write,
deduplication cleanup or embedding refresh was run.

## Repair

The prior scanner opened and initialized the destination database before all
participating instance and Agent configuration had been validated. It also
committed one source at a time and used the instance name in its deduplication
identity. A bad later source could therefore leave earlier writes committed,
and a moved or cloned workspace could duplicate the same source memory.

The local candidate now:

- accepts legacy BOM/CRLF JSON for source, Agent and skill-state reads;
- validates every configured instance and stable Agent/workspace mapping before
  opening the destination database;
- rejects an instance-identity mismatch and never infers an Agent identity from
  an arbitrary workspace directory name;
- warns and skips retired, unconfigured workspaces instead of importing them;
- performs the complete multi-instance scan under one `BEGIN IMMEDIATE`, with
  one final commit or a complete rollback;
- uses stable Agent/source/content identity across instances to suppress a
  copied memory after Move/Clone without deleting historical rows; and
- imports the optional embedding dependency lazily, so scan-only preflight does
  not require the embedding runtime.

The final local scanner SHA-256 is
`e3cea5471849b5f6ed824a155ae3f754b07c6744b37458e4547db061b8c693fd`.
The pre-repair file is retained as Git object
`7f8605fdb0fabbf3b54be2dff3f8ca416589c448`; it is not a public branch path.

## Verification and data boundary

The ignored focused test module uses only temporary instances and databases.
It covers BOM/CRLF compatibility, zero destination opens after a preflight
failure, rollback after a late malformed transcript, stable identity across
instances, configured-instance mismatch and safe handling of retired
workspaces. Result: **6 passed**.

A configuration-only preflight against the actual source registry succeeded:
HASHI1 had 17 active targets and two retired-workspace warnings, HASHI2 had
seven active targets and three warnings, and HASHI9 had one active target and
no warning. The target consolidated database was not opened by that check.

A separate read-only inventory found 75,370 existing rows, including 12,548
exact-content duplicate groups covering 29,059 rows. This is evidence for a
future reviewed cleanup, not authority to delete or rewrite any row. No cleanup
was attempted.

The protected-Core guard against the main baseline passed. Native Windows
adoption, an approved real scan and any historical cleanup remain separate
operator decisions. The scanner candidate must be copied and hash-verified on
each adopting machine because the source is intentionally instance-local.
