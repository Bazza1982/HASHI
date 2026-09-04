# Browser-Assisted Local Academic Revision Playbook

Use this playbook only for Barry's academic Word manuscripts on the HASHI
Windows desktop with the current Grammarly Docs interface.

Read these files before acting:

1. `../manifest.json`
2. `../config/review_policy.json`
3. `../failures/failure_memory.jsonl`
4. `../validators/grammarly_revision_validators.md`
5. the active AIRMS run record, if resuming

## Operating model

Keep authority and work in the right place:

`
immutable Word source
    -> local extraction, mapping, versioning, and revision
    -> one allowlisted batch in the logged-in browser
    -> Grammarly observation
    -> local analysis and candidate
    -> same complete batch returned for one formal recheck
    -> validated Word copy and checkpoint
`

The browser detects and displays evidence. It is not the editing workspace or
the system of record.

## Non-negotiable invariants

- Never overwrite the selected source DOCX.
- Never infer upload permission from local extraction.
- Never upload a unit absent from the positive allowlist.
- Never upload notes, references, tables, quotations, or mapping data unless
  Barry separately approves the exact unit.
- Never use Grammarly's AI Rewriter.
- Never enter, recover, expose, or record credentials.
- Never delete a Grammarly document, dismiss a finding, send feedback, or alter
  account settings without separate authority.
- Never widen a final marker beyond Grammarly's exact selected characters.
- Never call a batch score a whole-document score.
- Never describe a detector result as proof of authorship or misconduct.
- Never accept lower detector risk at the expense of meaning, evidence,
  citations, grammar, or Barry's voice.

## State machine

Use explicit states in the run packet:

1. `prepared_local`
2. `awaiting_upload_approval`
3. `approved_unit_only`
4. `r0_frozen`
5. `local_candidate_ready`
6. `replacement_verified`
7. `round_outcome_frozen`
8. `word_checkpoint_verified`
9. `clear`, `bounded_completion`, or `safe_stop`

Do not skip a state or infer that a later artefact proves an earlier gate.

## Phase A — Resume or create the local run

When resuming:

1. Read the latest checkpoint, batch manifest, run summary, and attempt ledger.
2. Recompute the immutable source hash.
3. Verify every referenced artefact exists and matches its recorded hash.
4. Reconcile state files. If the summary, manifest, and ledger disagree, stop
   forward work and append a reconciliation record before continuing.
5. Reuse existing provider evidence. Do not repeat a paid or external action
   merely because a summary is stale.

For a new run:

1. Copy `../templates/run_packet.md` into a confidential, untracked AIRMS run
   folder.
2. Record source path, SHA-256, size, modification time, provenance, intended
   output, and the user-selected correction limit `N`.
3. Create a named output target, but do not edit it yet.
4. Record the current EXP version and the hashes of this manifest, policy,
   playbook, validators, and failure memory.

## Phase B — Extract, classify, and map locally

1. Extract the Word body without altering the source.
2. Build a mapping ledger that identifies each source story, paragraph ordinal,
   section context, exact original text hash, distinctive beginning and ending,
   and protected inline elements.
3. Classify every unit:
   - eligible editable prose
   - local-only protected content
   - excluded structural content
4. Protect by default:
   - references and bibliography
   - title, headings, and standalone subheadings
   - tables and table cells
   - figures, charts, pictures, and captions
   - comments, headers, and footers
   - equations and non-prose objects
   - long direct quotations and required verbatim passages
   - notes, footnotes, and endnotes
5. Keep in-body citations, numbers, names, terminology, short meaning-bearing
   quotation fragments, and note anchors with their paragraph.
6. Verify extracted eligible and protected units against the Word package.

Do not insert artificial paragraph IDs, hashes, delimiters, or mapping labels
into text intended for Grammarly.

## Phase C — Build one bounded batch

Use the current policy values rather than relying on memory.

1. Group complete, ordered paragraphs into a target batch of 800–1,000 words.
2. Treat approximately 1,200 words as the ordinary upper bound.
3. Preserve natural section boundaries when possible.
4. Record a single indivisible-paragraph exception rather than splitting a
   paragraph or sentence.
5. Assign stable source mapping IDs and a batch ID.
6. Save:
   - exact raw local batch text
   - paragraph count and ordered mapping IDs
   - raw SHA-256
   - a canonical browser-comparison SHA-256
   - first and last paragraph fingerprints
   - word count

The canonical browser-comparison form may normalise only line separators and
known editor block boundaries. Record the algorithm and its version. It must
not normalise spelling, punctuation, whitespace inside sentences, citations, or
words.

Only one batch may be active externally.

## Phase D — External approval gate

Before creating or changing a Grammarly document, tell Barry:

- exact source and source hash
- exclusions and protected units
- active batch ID, paragraph count, mapping range, and word count
- exact local file and SHA-256 proposed for upload
- selected `N`
- intended Grammarly document name
- intended local and Word outputs
- whether any disposable editor-replacement preflight is required

Obtain explicit approval for the exact upload unit and browser-side action.
Write that approval into the allowlist. Approval for one batch does not extend
to later batches or local-only units.

## Phase E — Verify browser and editor replacement control

1. Connect to the user's real Windows Chrome through the HASHI Browser Bridge.
2. Confirm visible authenticated Grammarly Docs state.
3. If login, account, workspace, or extension state is ambiguous, stop.
4. Do not rely on dynamic element IDs from an earlier page load.

Before using real manuscript text, prove whole-editor replacement on disposable
text when the current editor/tool combination has not already passed this
preflight:

1. Use a separately authorised temporary document containing at least three
   distinct paragraphs.
2. Verify the text occurs exactly once and the first/last fingerprints match.
3. Attempt full-editor selection using a method whose visible selection spans
   every paragraph.
4. Replace it with a second known three-paragraph version.
5. Reconstruct the editor text and require exact canonical hash, paragraph
   count, first/last fingerprints, and unique occurrence checks.
6. Reload and repeat the integrity check after autosave settles.

`Ctrl+A` or a successful typing call is not proof of full-editor selection.
If selection collapses, only one block is selected, text lands at a stale
caret, or the replacement cannot be reconstructed exactly, undo the bounded
change if safe and stop. Do not learn a replacement method on a manuscript.

A second paired document is not an automatic fallback. It changes the
experimental unit and introduces a document-level confound. Use it only after
Barry approves a run amendment, and label causal comparisons accordingly.

## Phase F — Create and verify R0

After approval and replacement control:

1. Create or reuse the active batch document.
2. Set a recognisable title using the visible title control and verify the
   visible result.
3. Paste only the exact allowlisted R0 batch.
4. Wait on observable readiness, not a fixed delay:
   - stable URL and title
   - settled loading/autosave state
   - exact first and last paragraphs
   - expected paragraph count
   - unique occurrence of each paragraph where feasible
   - consistent word count
   - canonical browser text hash equals local hash
5. Treat the displayed word counter as a supporting check only.
6. Reload once when needed and repeat integrity checks.
7. If any check fails, do not run the detector. Preserve evidence and recover or
   safe-stop.
8. Open the AI Detector and freeze R0:
   - exact visible label and percentage
   - total and severity counts
   - exact marked ranges and order
   - batch text hash and mapping IDs
   - URL, title, time, and evidence image

A clear R0 still needs final extraction and local validation; it simply needs no
rewrite.

## Phase G — Freeze findings

Before any local revision:

1. Give every finding a stable local issue ID.
2. Capture its exact selected substring and distinguishing left/right context.
3. Map it to the source paragraph.
4. Record the full local paragraph in the confidential run, not in EXP.
5. Record finding type, score, severity, and screenshot reference.
6. Capture Grammarly's explanation verbatim:
   - hover the marker
   - use rendered text when already available
   - click `Show explanation` only when necessary
   - poll for up to roughly 15–20 seconds
   - make at most one bounded operational retry
   - record `explanation_unavailable` if it still fails

Never invent or paraphrase a missing provider explanation. Do not dismiss the
finding.

For a production round, freeze the complete active finding set before revising.
A smaller proof-of-concept target set is allowed only when the run packet
records Barry's explicit experimental amendment.

## Phase H — Revise locally

For each targeted paragraph:

1. Copy `../templates/rewrite_attempt.md` into the confidential attempt
   ledger.
2. Diagnose the provider-described pattern in the paragraph's real argumentative
   context.
3. Draft the complete replacement locally.
4. Record exact deletions, additions, reordering, splitting, merging,
   specification, qualification, and word-count change.
5. Apply the academic-quality gate:
   - same factual meaning and degree of claim
   - same citation-to-claim relationships
   - names, numbers, terminology, quotations, and note anchors preserved
   - no invented evidence, source, jurisdiction, participant, or result
   - grammatical and professional academic English
   - Barry's voice and paragraph function preserved
   - no deliberate spelling or grammar error
   - no unnecessary expansion
6. Reject the candidate locally if any gate fails. Keep the rejected candidate
   and reason in the ledger.
7. Build the exact complete next-round batch locally and hash it.

Prefer situated claims, explicit actors, natural sentence variation, and
argument-specific structure when those changes remain true to the manuscript.
Do not substitute synonyms mechanically or chase a score.

## Phase I — Replace the complete batch and reassess once

1. Return to the same active Grammarly document.
2. Confirm the current editor still equals the frozen prior-round hash.
3. Select the complete editor only through the preflight-proven method.
4. Replace it once with the exact next-round batch.
5. Verify exact canonical hash, ordered paragraphs, first/last fingerprints,
   unique occurrences, word count, URL, title, and settled autosave.
6. Do not run detection in an anomalous state.
7. Run one formal AI Detector assessment for the round.
8. Freeze the complete new result before any further revision.
9. Compare every prior target by exact or overlapping range:
   - `cleared-tentative`
   - `persisted`
   - `narrowed`
   - `shifted`
   - `reappeared`
   - `worsened`
   - `new`
   - `indeterminate`
10. A lower batch percentage alone does not establish target-level success.
11. Repeat local revision and one formal reassessment until stable clear or
    `N` is reached.
12. At final assessment, upgrade a still-absent tentative clearance to
    `cleared-stable`.

If Barry changes `N`, record the old value, new value, time, reason, and
effective round.

## Phase J — Write back one completed batch

Do not start the next external batch yet.

1. Extract only the complete editor body for the final batch version.
2. Verify it against the final local batch hash and mapping.
3. Create or update only the named Word working/output copy.
4. Replace mapped paragraphs at their exact positions.
5. Preserve all excluded and local-only units unchanged.
6. Preserve styles, citations, fields, hyperlinks, numbering, notes, comments,
   and other package members unless separately authorised.
7. For each unresolved final finding:
   - map the exact selected characters
   - apply orange `#F4B183` only to those characters
   - anchor the comment to the identical character range
   - quote the explanation verbatim
   - keep AIRMS metadata visibly separate from the quotation
8. If exact placement is ambiguous or explanation evidence is missing, stop
   annotation rather than approximate.
9. Reopen the output in real Microsoft Word.
10. Run every applicable validator and compare protected content.
11. Save the output hash and a recoverable checkpoint.

Only after `word_checkpoint_verified` may the next batch be proposed for
approval.

## Phase K — Close and learn

1. Reconcile summary, batch manifest, attempt ledger, screenshots, hashes, and
   final state.
2. Record start/final scores, rounds, stable clears, unresolved findings,
   indeterminate outcomes, and stopped checks.
3. Record both successful and failed candidates.
4. Keep exact text in the confidential AIRMS run.
5. Generalise only evidence-backed lessons into
   `../evidence/strategy_register.md`.
6. Append every meaningful new operational failure to
   `../failures/failure_memory.jsonl`.
7. Follow `train_and_promote.exp.md` before changing lifecycle status.

## Stop conditions

Safe-stop immediately when:

- browser authentication or account identity is uncertain
- source, batch, or mapping hashes disagree
- upload scope or approval is ambiguous
- pasted/replaced text is duplicated, missing, reordered, or unmappable
- full-editor replacement is not proven
- the provider explanation required for an annotation is unavailable
- the exact marked range cannot be mapped uniquely
- a candidate fails academic quality
- Word output or protected content fails validation
- run records disagree and cannot be reconciled without guessing

Preserve the last verified state and report the exact next safe action.
