# Auto Paper Edit Superloop Template

## Purpose

Run a fully automated academic paper editing loop from an edit request and
working draft through planned changes, execution, consistency sweep, and a
clean final Word document ready for submission.

This template implements the Paper Editing Library Service SOP
(WORKSPACE_SOP.md v1.6) end-to-end. It supports both:

- **Route A** — fine-grained per-paragraph edits (edit_paragraph.py)
- **Route B** — bulk markdown injection (4-step stamp → inject → verify → strip pipeline)

The loop does not close when the editor claims completion. It closes only
after the reviewer independently confirms the final Word document matches
the edit request, with no registry anomalies or consistency violations.

---

## Required Roles

- `orchestrator`: owns the edit plan, scope, quality bar, locked paragraphs
  list, and final acceptance. Reads the registry report before any edits
  begin. Must personally inspect the final document.
- `editor`: executes all edits following the SOP. Runs scripts, updates
  the registry, and produces the working copy. Does not self-accept.
- `reviewer`: Akane or equivalent independent agent. Reviews the edit
  plan before execution and reviews the final document after execution.
  Gives an explicit no-blocker verdict before closeout.

---

## Inputs (required at loop start)

- **Edit request**: what needs to change and why (may be a memo, a list of
  instructions, or a set of paragraph IDs with change instructions)
- **Working copy path**: absolute path to the `.docx` working copy
- **Route**: `A` (per-paragraph) or `B` (bulk markdown injection)
- **Route B only**: path to `assembled_draft_vN.md` and confirmation that
  it contains zero `══` lines
- **Locked items list**: sections/paragraphs that must not be touched
  (PRISMA numbers, AAAJ abstract structure, data-locked tables)
- **Exit condition**: what does "done" look like? (e.g. all UPDATE_NEEDED
  paragraphs resolved, specific sections replaced, review copy approved)

---

## Non-Negotiable Gates

### G1 Session Start Gate

Before any work begins, the orchestrator must verify:

1. `WORKSPACE_SOP.md` is read and the current version is confirmed.
2. `EDIT_PLAN.md` exists and pending phases are identified.
3. The canonical `.docx` path is confirmed accessible.
4. The active sidecar registry (`[docx-stem]_registry.json` next to the
   `.docx`) is identified — NOT any `paragraph_registry.json` inside
   `_workspace/`.

Any misplaced registry file must be moved to `_workspace/backups/` with a
`_LEGACY_` timestamp before proceeding.

### G2 Registry Audit Before Edits

The orchestrator runs `registry_report.py` and records:

- Total paragraph count (active / deleted)
- Status breakdown (STABLE / LOCKED / UPDATE_NEEDED / INSERT_NEEDED)
- Paragraphs with Word comments (edit-risk: comment protection required)
- Consistency map entries (outbound mentions, inbound dependencies)

No editing begins until the orchestrator has a current registry snapshot
and the edit plan is aligned with actual registry state.

### G3 Edit Plan Lock

Before the editor starts:

- `EDIT_PLAN.md` must list every planned change in execution order.
- The reviewer must approve the plan (G5 pre-edit review) before
  execution begins.
- LOCKED paragraphs are explicitly listed. The editor must refuse any
  instruction that touches a LOCKED paragraph and escalate to the
  orchestrator.

### G4 Route Selection and Pre-flight

**Route A:**
- Every target paragraph UUID must exist in the active registry.
- Paragraphs with `word_has_comment: true` require comment protection
  protocol (extract anchors → edit → re-inject) — not optional.
- Edit mode (`DIRECT` or `TRACKED`) must be declared before execution.

**Route B:**
- `assembled_draft_vN.md` must contain zero `══` separator lines (Guard 1).
- `replacement_map.json` must be 100% confirmed (`confirmed: true` for
  every entry) before `route_b_interleaved.py` runs (Guard 2).
- Anchor paragraph UUID must be confirmed in the registry before injection.
- Figure placement map must be confirmed: `[FIGURE N]` → filename → section.
- A timestamped backup must exist in `_workspace/backups/` before injection.

### G5 Pre-Edit Reviewer Approval

The reviewer inspects the edit plan and confirms:

- Changes align with the stated edit request.
- No LOCKED paragraphs are in scope.
- Consistency map dependencies are noted (paragraphs that will need review
  after the primary edits).
- Route B: replacement_map entries make sense; no accidental whole-section
  deletions.

The reviewer gives explicit APPROVE or BLOCK. A BLOCK is a hard stop.

### G6 Execution and Per-Phase Validation

**Route A:**

After each paragraph edit:
- Registry updated: `changed = true`, `status = STABLE`, `change_date` set.
- EDIT_PLAN item marked done.
- If the edited paragraph has outbound `mentions` or is referenced by
  `depends_on` entries, those dependent paragraphs are flagged for
  consistency review.

**Route B:**

After `route_b_interleaved.py`:
- `verify_injection.py` must exit 0 with all 8 checks PASS.
- `extract_to_markdown.py --validate` must output `Heading sequence OK`.
- `review_registry.json` must contain all four sections: `paragraphs`,
  `preserved_from_original`, `deleted_from_original`, `summary`.
- `summary.total_accounted_for` must equal `inserted + preserved + deleted`.
- Zero orphan element warnings (or each orphan reviewed and approved by
  orchestrator).

**Phase insertion guard (Route B):**
After any phase that uses `insert_paragraph.py`, run
`extract_to_markdown.py --validate` before moving to the next phase.

### G7 Consistency Sweep

After all edits in a phase are complete:

- Run `consistency_check.py` against all changed paragraph UUIDs.
- The editor produces a list of flagged dependent paragraphs.
- The orchestrator reviews each flagged paragraph and either:
  - Marks it as reviewed-no-action-needed, or
  - Adds it to the next edit cycle.
- The phase does not close until the consistency sweep is complete.

### G8 Final Document Verification

Before calling the loop done, the orchestrator must verify the output
document directly:

**Route A output (working copy.docx, DIRECT mode):**
- `registry_report.py` shows zero UPDATE_NEEDED / INSERT_NEEDED paragraphs
  (or all outstanding items are confirmed out-of-scope for this session).
- No paragraphs show unexpected status regression.

**Route B output (working copy.docx → submission copy.docx):**
- Working copy: `verify_injection.py` 8/8 PASS, heading sequence OK.
- Run `strip_stamps.py` to produce `submission copy.docx`.
- `strip_stamps.py` self-verifies: exits 0, zero stamps, zero review
  highlights remaining.
- Submission copy file size is reasonable (not empty, not truncated).

### G9 Reviewer Final Inspection

The reviewer independently opens the output document and checks:

- All planned edit items from `EDIT_PLAN.md` are present and correct.
- No unintended changes exist (green content is only where expected).
- No LOCKED content was altered.
- No figure or table is missing.
- No heading sequence is broken.

Reviewer gives explicit no-blocker verdict. Any blocker reopens the loop.

### G10 Closeout

The loop closes only when:

- All EDIT_PLAN phases are marked DONE.
- Consistency sweep is complete and all flagged items resolved or deferred.
- Registry is synced to document state.
- Final document verification (G8) passes.
- Reviewer no-blocker verdict (G9) is received.
- No open blockers remain in the issue log.
- `session_notes.md` has an end-of-session summary entry.
- `EDIT_PLAN.md` is fully up to date.
- `[docx-stem]_insert_session.json` is still present (chain guard state).

---

## Standard Taskboard

1. Session start gate: verify SOP, EDIT_PLAN, canonical docx, registry path.
2. Orchestrator runs `registry_report.py` and records current document state.
3. Orchestrator writes or updates `EDIT_PLAN.md` with all planned changes.
4. Reviewer pre-edit plan review (G5) — APPROVE or BLOCK.
5. Route pre-flight checks (G4).
6. Editor executes Phase 1 edits (per EDIT_PLAN order).
7. Consistency sweep for Phase 1 (G7).
8. Phase insertion validation if applicable (route_b or insert_paragraph).
9. Repeat steps 6–8 for remaining phases.
10. Final document verification (G8).
11. Reviewer final inspection (G9).
12. Fix any reviewer blockers and retest.
13. End-of-session sync: EDIT_PLAN, registry, session_notes, chain guard check.
14. Closeout.

---

## Report Schema

```text
report_type:           (ORCHESTRATOR_INTAKE | EDITOR_PHASE | REVIEWER_PRECHECK
                        | REVIEWER_FINAL | ORCHESTRATOR_CLOSE)
loop_id:
role:
docx_path:
registry_path:
route:                 A | B

# --- INTAKE ---
edit_request_summary:
locked_items:
registry_snapshot:     (output of registry_report.py at loop start)

# --- PLAN ---
edit_plan_phases:
consistency_dependencies_noted:

# --- EXECUTION (per phase) ---
phase_id:
paragraphs_changed:    [uuid, ...]
paragraphs_inserted:   [uuid, ...]
paragraphs_deleted:    [uuid, ...]
verify_injection_pass: yes | no | not_applicable
heading_sequence_ok:   yes | no | not_applicable
consistency_flagged:   [uuid, ...]
consistency_resolved:  [uuid, ...]

# --- REVIEW ---
plan_verdict:          APPROVE | BLOCK
final_verdict:         no_blockers | BLOCK
blockers:

# --- CLOSEOUT ---
edit_plan_done:        yes | no
registry_synced:       yes | no
submission_copy_clean: yes | no | not_applicable
open_issues:
next_session_notes:
```

---

## Anti-Patterns

- Starting edits before `registry_report.py` has been run.
- Reading `_workspace/paragraph_registry.json` as current state (it is a
  legacy snapshot).
- Editing LOCKED paragraphs without orchestrator escalation.
- Skipping the comment protection protocol on `word_has_comment: true`
  paragraphs.
- Reporting Route B complete without checking `verify_injection.py` output.
- Running `strip_stamps.py` before `verify_injection.py` 8/8 PASS.
- Skipping the consistency sweep and going straight to close.
- Closing while reviewer has unresolved blockers.
- Leaving `_workspace/` with loose files outside the defined structure.
- Accepting `assembled_draft_vN.md` that contains `══` lines as clean input.
- Treating a partial replacement_map (unconfirmed entries) as safe to run.
