# Grammarly Academic Revision Validators

Run the strongest applicable checks before advancing state. A green browser
score is never sufficient by itself.

## 1. Source and authority gate

- The exact source DOCX path, SHA-256, size, and modification time are recorded.
- The source hash still matches before and after the run.
- The intended output is a separately named DOCX.
- The external-review scope is explicit.
- The active upload file is present on a positive allowlist.
- Approval names the exact batch/unit and browser-side action.
- Notes, references, tables, mapping data, and other protected units are absent
  from the allowlist unless separately approved.
- The active EXP manifest version and policy/playbook/validator hashes are
  recorded.

Failure of any item blocks upload.

## 2. Local extraction and mapping

- Every eligible paragraph has one stable source mapping identity.
- Eligible and protected/excluded inventories reconcile to the source.
- Ordered eligible text matches the source after only the recorded extraction
  normalisation.
- In-body citations, numbers, names, terms, quotation fragments, and note
  anchors are represented correctly.
- Beginning/ending fingerprints and original hashes are recorded.
- No artificial paragraph IDs or mapping delimiters occur in sendable text.

Ambiguous mapping requires `safe_stop`.

## 3. Batch construction

- Batch boundaries occur between complete paragraphs.
- Word count is within the configured target/upper bound, or a recorded
  indivisible-paragraph exception exists.
- Batch ID, ordered mapping IDs, paragraph count, raw hash, canonical comparison
  hash, word count, and first/last fingerprints are recorded.
- Only one external batch is active.
- The result is labelled batch-only.

## 4. Browser and paste integrity

Before detection and after every replacement:

- Authenticated Grammarly Docs state is visibly confirmed.
- The intended workspace/document identity is confirmed.
- Stable URL and visible title are recorded.
- First and last paragraphs match the local batch.
- Paragraph order and count match.
- The browser reconstruction canonical hash matches the local version.
- Paragraphs do not appear duplicated.
- The displayed word count is consistent, but is not the sole integrity check.
- Loading/autosave has settled based on page state.
- A reload check passes when the run design requires it.

Do not run the detector in an integrity-uncertain editor.

## 5. Complete-editor replacement preflight

Before real R1 replacement:

- A separately authorised disposable document contains at least three distinct
  paragraphs.
- Visible selection spans the entire editor, not one block.
- Replacement produces the exact expected canonical hash and paragraph order.
- First/last fingerprints and unique occurrence checks pass.
- The result still passes after reload/autosave.
- One known failed-selection condition is detected and safely recovered.

If this preflight has not passed for the current editor/tool combination,
production same-document replacement remains blocked.

## 6. Frozen detector evidence

For R0 and each formal round:

- Exact batch version hash is recorded.
- Visible result wording, percentage, severity totals, marker count, URL,
  title, and timestamp are recorded.
- All findings are frozen before editing unless a user-approved POC subset is
  explicitly documented.
- Every finding has a stable local issue ID.
- Exact selected text and distinguishing context are saved.
- Every finding maps uniquely to a local paragraph.
- Verbatim explanation or an explicit `explanation_unavailable` state is
  saved.
- Evidence screenshots or equivalent rendered-page evidence exist.
- Dynamic provider IDs are treated as provenance only, not durable identity.

## 7. Candidate academic-quality gate

For every proposed candidate:

- Complete before/after paragraph text is retained in the confidential ledger.
- Diagnosis and chosen strategy precede browser insertion.
- Exact changes and word-count change are recorded.
- Meaning and degree of claim are preserved.
- Citation-to-claim relationships are preserved.
- Names, numbers, terms, quotations, and note anchors are preserved.
- No evidence, source, jurisdiction, participant, result, or factual assertion
  is invented.
- Grammar and professional academic English are acceptable.
- Barry's voice and the paragraph's argumentative function remain intact.
- No deliberate grammar or spelling error is added.
- Rejected and superseded candidates remain in the ledger.

An academically defective candidate fails even if the provider later clears it.

## 8. Round comparison

- The complete prior-round editor hash matched before replacement.
- The same ordered batch and same Grammarly document were used, unless an
  explicitly approved amendment records the confound.
- Only recorded candidate changes differ between versions.
- Exactly one formal detector reassessment was initiated for the round.
- The new complete finding set is frozen before further editing.
- Every prior target receives a defined local outcome:
  `cleared-tentative`, `persisted`, `narrowed`, `shifted`,
  `reappeared`, `worsened`, `new`, or `indeterminate`.
- A falling percentage is not used as sole evidence of a local clear.
- Tentative clears are upgraded to stable only at final assessment.
- The configured/user-selected round limit is respected.

## 9. Word write-back

- The source DOCX remains unchanged.
- Only the named output/working copy was written.
- Final local batch hash equals the final browser extraction hash.
- Every changed paragraph was returned to its exact source position.
- References, headings, tables, figures, captions, quotations, notes, comments,
  headers/footers, equations, fields, and other protected package members match
  the source.
- Citations, numbering, hyperlinks, styles, and note anchors remain correct.
- Every unresolved orange highlight equals the exact final provider range.
- Every unresolved comment is anchored to that identical range.
- Every provider explanation is quoted verbatim and separated from local
  metadata.
- No unresolved annotation was widened for convenience.
- The output opens and saves in real Microsoft Word.
- The output hash and application-validation evidence are recorded.

## 10. Record reconciliation

- Run summary, batch manifest, allowlist, browser session, attempt ledger, round
  outcomes, evidence index, and Word checkpoint describe the same latest state.
- Every path referenced as evidence exists.
- Every recorded hash can be recomputed.
- Paid/external requests are not repeated merely to repair record drift.
- Exact manuscript text remains outside Git-tracked EXP files.
- The strategy register receives both positive and negative evidence.
- Meaningful operational failures are appended to failure memory.

## 11. Completion and lifecycle

Valid operational outcomes:

- `clear`
- `bounded_completion`
- `safe_stop`

Only the first two count as completed batches, and only when Word validation
passes.

Do not promote lifecycle status unless the gates in
`../playbooks/train_and_promote.exp.md` are satisfied. Barry's explicit
confirmation is mandatory for `stable`.
