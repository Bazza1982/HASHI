# Auto Journal Watch Superloop Template

## Purpose

Run a journal watch cycle from initial journal-period scan through selection,
download, library filing, Zotero linking, Zotero notes, search-index sync, and
final reconciliation.

This template is for recurring academic literature monitoring where the loop
must follow the Journal Watch and Ex-portario SOPs, preserve a strict manifest
boundary, and keep human intervention limited to explicit approval or access
events such as Okta, captcha, platform policy changes, or final selection.

The template closes only when the selected rows, unique papers/PDFs, Zotero
records, notes, search index, and final reconciliation are all accounted for.

## Required Roles

- `orchestrator`: Sakura or the active controller. Owns state, taskboard,
  waits, issues, escalation, role dispatch, continuation decisions, and final
  closeout. The orchestrator is the control plane only.
- `librarian`: Kurage or the approved library agent. Executes discovery,
  acquisition, download, Zotero, note, and search-index service steps.
- `reviewer`: Momo or the approved reviewer agent. Reviews every gate and
  evidence bundle. The reviewer advises; it never owns continuation decisions.
- `human`: the configured human approver. Handles MFA, captcha, manual access decisions, policy
  exceptions, final selection, and any approved role reassignment.

Do not substitute a different librarian or reviewer unless the human explicitly
approves reassignment and the taskboard records the change.

## SOP Sources

The loop must record the exact SOP versions or file fingerprints used at start:

- `00_main_library/journal_watch/SOP__Journal_Watch__Library_Human_Navigation_Workflow__v1.md`
- `00_main_library/ex-portario/SOP__Ex_portario__Library_Fulltext_Download_Workflow__v1.md`
- `00_main_library/ex-portario/PLATFORM_ROUTE_STATUS__Ex_portario__v1.json`
- `00_main_library/SOP__Okta_Authentication_Service__v1.md`

## Inputs

- Watch window, e.g. `2026-01-01` to `2026-03-31`.
- Journal scope and allowed discovery mode.
- Output roots for reviewer bundles, manifests, downloaded PDFs, Zotero notes,
  Docling markdown, sectioned markdown, and search DB.
- Human selection policy, e.g. top-N or explicit selected rows.
- Approved platform policy, including paused hosts.
- Approved librarian and reviewer agent identities.
- Exit condition, including row coverage and unique-paper coverage.

## Non-Negotiable Gates

### G1 Manifest-Bounded Execution

Every executable batch must be driven by a manifest. Scripts must not infer
targets by sweeping all PDFs, all markdown files, all Zotero items, or the whole
library unless the task is explicitly a whole-library maintenance job.

Required preflight before any batch:

- manifest path
- manifest row count
- already completed count
- planned target count
- exact planned target list
- output directory
- route/platform distribution when downloads are involved

Fail fast if the planned target count exceeds the manifest count.

### G2 Verified Role Dispatch

The orchestrator may only report agent contact after evidence exists.

Allowed states:

- `dispatch_prepared`: message drafted but not delivered.
- `dispatch_delivered`: delivery is verifiable.
- `reply_received`: the agent replied with usable output.
- `reply_reviewed`: the output was inspected and classified.

Do not say the librarian or reviewer was contacted without delivery evidence.

### G3 Reviewer Is Advisory

The reviewer checks SOP compliance, evidence, and risks. The orchestrator makes
the continuation decision after reading the review. A reviewer approval is not a
substitute for orchestrator closeout.

### G4 Long-Running Heartbeat

Any long task must maintain heartbeat evidence with:

- PID or session identity when available
- current item or current phase
- completed, failed, skipped, and pending counts
- log path
- status path
- last update timestamp

No silent wait may exceed 300 seconds. If the heartbeat is stale, pause,
classify, and report the state instead of assuming progress.

### G5 Final Selection Duplicate Audit

After human selection and before Ex-portario handoff, run a final duplicate
audit on the selected set. This audit must include normalized title matching and
author normalization, not DOI-only matching. No-DOI duplicates are expected.

The loop must report both:

- selected row coverage
- unique paper/PDF/markdown/note processing count

Example: `50 selected rows covered; 49 unique PDFs processed`.

### G6 Platform Safety

Use the Ex-portario platform route registry instead of hardcoded platform
assumptions. If a host shows captcha, Cloudflare, purchase-only access, robotic
click risk, or session loops, escalate or move to a safer route according to the
SOP. Do not repeatedly hit a fragile host.

### G7 Search DB Safety

Search-index updates must use the scoped DB-only sync path for the current
batch. For Journal Watch runs, prefer the batch tag and known query DB path.
Do not accidentally run a whole-library sync because a help command or missing
argument fell through to a default behavior.

## Standard Lifecycle

1. Intake and run charter.
2. SOP/version capture.
3. Auth and browser preflight.
4. Journal-period discovery plan.
5. Reviewer discovery-plan gate.
6. Discovery execution.
7. Discovery artifact review.
8. Clean, rebuild, Crossref enrich, and AI triage.
9. Reviewer triage bundle review.
10. Human selection gate.
11. Selected-set duplicate audit and already-have check.
12. Reviewer selection-lock review.
13. Acquisition plan and platform split.
14. Reviewer acquisition-plan review.
15. Download execution batches.
16. Waits and human escalations.
17. Download reconciliation, including late arrivals and manual PDFs.
18. Reviewer download reconciliation.
19. Processing manifest build with row-vs-unique counts.
20. Reviewer processing-manifest gate.
21. Docling/raw markdown extraction.
22. Section cleanup.
23. Library filing and Zotero attachment linking.
24. Main library index rebuild.
25. Zotero note generation.
26. Scoped search DB sync.
27. Final reconciliation pack.
28. Reviewer final closeout.
29. Orchestrator final decision: `completed`, `blocked_human`,
    `blocked_issue`, or `paused`.

## Modular Service Boundaries

Keep the superloop core minimal. The core owns only state, taskboard entries,
waits, issues, events, and role dispatch. Feature work belongs in service
adapters that can be swapped or upgraded independently:

- discovery adapter
- Crossref/enrichment adapter
- AI triage adapter
- Ex-portario route adapter
- platform download adapter
- Docling extraction adapter
- section cleanup adapter
- Zotero API adapter
- Zotero note adapter
- search DB sync adapter

Manifests should be versioned and forward-compatible. New optional fields must
not break older consumers. Route behavior should live in external registries,
not in the orchestration core.

## Logging And Evidence

Each loop instance should maintain:

- `state.json`
- `taskboard.json`
- `events.jsonl`
- `issues.json`
- `waits.json`
- preflight artifacts
- execution logs
- reconciliation JSON/CSV/Markdown
- role dispatch and receipt records
- final closeout report

Every task should link to concrete artifacts instead of relying on narrative
claims.

## Human Escalation Matrix

Escalate to `human` for:

- Okta push or MFA.
- Browser login expiry.
- Captcha or Cloudflare.
- Site policy or access route change.
- Repeated fragile-host failures.
- Purchase-only access.
- Manual PDF supply.
- Final selection decisions.
- SOP changes.
- Agent role reassignment.
- Counts that do not reconcile.

## Closeout Criteria

The loop can close as `completed` only when:

- selected rows are fully accounted for
- unique papers/PDFs are fully accounted for
- duplicate row pairs are documented
- downloads or manual PDFs are reconciled
- PDFs have valid names and are filed in the library
- Zotero attachments are linked where required
- Zotero notes are generated where required
- main library index is rebuilt where required
- search DB sync is complete for the batch
- reviewer has reviewed the final pack
- orchestrator has independently accepted the final evidence
