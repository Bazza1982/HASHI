# Auto Paper Draft Superloop Template

## Purpose

Run a fully automated academic paper drafting loop from an initial research
idea through all analytical and writing stages to a publication-ready full
draft, ready for handoff to the Citalio reference injection service.

This template implements the Paper Drafting Service SOP
(`00_main_library/SOP__Paper_Drafting_Service__v1.md`) end-to-end — all 9
stages. It does **not** include reference injection (Citalio, Service 20)
or Word document formatting (Paper Editing Service, Service 21). Those are
separate downstream handoffs.

The loop does not close when the librarian claims the draft is complete. It
closes only after the reviewer independently confirms template compliance,
quote provenance, and anonymisation integrity across all sections.

---

## Required Roles

- `orchestrator`: Owns the pipeline, scope, quality bar, anonymisation rules,
  gate decisions, and final acceptance. Does not do the writing. Must
  personally verify template compliance before closeout.
- `librarian`: Research and writing agent. Owns Stages 1–9: research idea
  validation, literature review, writing template adoption, method design,
  coding plan, qualitative coding, inter-coder verification, NVivo package,
  evidence packaging, and all section drafts through to full paper assembly.
  Does not self-accept at any gate.
- `reviewer`: Independent quality agent. Reviews at five mandatory gates:
  (G2) LR quality, (G5) pre-coding independence, (G6) coding quality,
  (G7) evidence package, (G8) section drafts, and (G10) final full paper.
  Does not write. Gives explicit APPROVE or BLOCK at each gate.

---

## Inputs (required at loop start)

- **Research idea**: researcher's conceptual interest, observed gap, or
  empirical puzzle — may be a paragraph, a brief, or a `Research Idea.md` file
- **Paper folder**: absolute path to the `04XX_paperX_[Topic]/` folder that
  will house all outputs
- **Theoretical lens**: which analytical framework will govern coding
  (e.g., Lacanian Discourse Analysis, Grounded Theory, Thematic Analysis)
- **Empirical data**: confirmation that interview transcripts or equivalent
  qualitative data exist and are accessible; approximate participant count
- **Writing template**: the published paper to use as structural template
  (e.g., Gaudy 2023) or a journal-specific template file
- **Anonymisation policy**: confirm whether participant codes are already
  assigned or whether the librarian must generate them
- **Exit condition**: what does "done" look like? (e.g., all 9 stages
  complete, full draft assembled, template compliance verified, SOP updated)

---

## Non-Negotiable Gates

### Hard State Machine

Every `auto_paper_draft` run must follow the hard state machine in:

```text
state_machine.template.json
```

The core rule is:

```text
No state transition without external evidence.
```

The orchestrator may not treat a local dispatch file as worker acceptance, may
not treat seed material as worker output, and may not close a loop without a
reviewer verdict. The required sequence is:

```text
dispatch_prepared
-> hchat_confirmed
-> worker_artifact_present
-> orchestrator_harvested
-> reviewer_dispatched
-> reviewer_verdict_present
-> closeout_allowed
```

Any skipped state is a blocker.

Before starting, read the shared orchestration guidance:

```text
superloops/config/orchestration_guidance.json
```

### G1 — Research Idea Scope Lock

Before any literature review work begins, the orchestrator must confirm:

1. `Research Idea.md` exists in the paper folder with all 6 fields:
   problem statement, RQ, theoretical lens, empirical context, significance,
   anticipated contributions.
2. The paper folder structure matches the SOP template.
3. Anonymisation policy is established — participant codes confirmed or
   generation plan agreed.
4. The writing template paper has been identified (e.g., Gaudy 2023).
5. Exit condition is recorded in loop state.

A missing or incomplete Research Idea is a hard stop. Do not proceed to LR.

### G2 — Literature Review Quality Gate

After LR1 and LR2 drafts are complete, the reviewer independently checks:

- LR1 (theoretical framework): positions the paper's theoretical
  contribution; ends by naming the gap this paper fills.
- LR2 (empirical domain): positions the paper's empirical contribution;
  ends by naming the gap this paper fills.
- At least 3 anchor paper deep reading notes are present in
  `Paper_specific_literature/`.
- Both LRs are at v2+ (iterated, not first-pass drafts).

Reviewer gives explicit APPROVE or BLOCK with written rationale. A BLOCK
is a hard stop — LR must be revised before Stage 3 begins.

### G3 — Writing Template Confirmed

After Stage 3 (writing template adoption), the orchestrator confirms:

- `[Template]_DeepReading_Gate.md` exists with section-by-section analysis.
- `[Template]_Writing_Template.md` exists with annotated word count targets,
  rhetorical moves, and structural constraints for every section.
- All subsequent drafting will be governed by this template.

This gate is orchestrator-only — no reviewer needed. Hard stop if template
files are missing.

### G4 — Method and Anonymisation Gate

After Stage 4 (method design), the orchestrator confirms:

- `research_notes/Method_Research_Notes.md` records all key design decisions.
- `PARTICIPANT_ANONYMISATION_MAP.md` exists with all participant codes assigned.
- `drafts/Method_v1.md` exists as a first complete draft.
- Real participant names do not appear in any output file.

This gate is orchestrator-only. Hard stop if anonymisation is incomplete
before any analytical memo is written.

### G5 — Pre-Coding Independence Gate

Before any transcript is read analytically, the reviewer independently checks:

- `research_notes/PreCoding_Reflection_Memo.md` is present and complete.
  It must have been written *before* any transcript was read analytically.
- The memo covers: anticipated findings, what would surprise the researcher,
  institutional position relative to participants, known analytical risks.
- `research_notes/LDA_Coding_Plan.md` (or equivalent) is complete.
- The reviewer confirms that the primary coder has documented their priors
  and that the memo is credible as a pre-coding document.

A missing or post-hoc pre-coding memo is a hard stop. The memo cannot be
written after coding has begun — if this is discovered, the orchestrator must
escalate to the researcher before proceeding.

### G6 — Coding Completion and Quality Gate

After all per-participant analytical memos, cross-case memo, inter-coder
verification, and NVivo coding package are complete, the reviewer checks:

- At least one memo per participant exists in `research_notes/analytical_memos/`.
- Memos are analytical arguments, not summaries (reviewer spot-checks 3 memos).
- `INTER_CODER_COMPARISON.md` records convergence, productive disagreement,
  and genuine divergence with resolution notes.
- Independent coder memos confirm the independent coder did not access the
  primary coder's memos before completing their own.
- NVivo package: all 4 files present (CODEBOOK, SOURCE_LIST, THEMES_OVERVIEW,
  INTERCODER_SUMMARY) plus coded excerpts for all participants.
- Reviewer identifies whether the themes as named are analytically distinct
  and theoretically grounded.

Reviewer gives explicit APPROVE or BLOCK. A BLOCK means coding is
insufficient — do not begin evidence packaging.

### G7 — Evidence Package Gate

After Stage 7 (evidence packaging), the reviewer checks:

- One `THEME[N]_EVIDENCE_PACKAGE_v2.md` per theme exists.
- Each package contains: theme name + theoretical claim, participant
  evidence ranked by analytical weight (Tier 1 / Tier 2), verified original
  quotes, analytical commentary, theme-framework connection, and gaps/counter-cases.
- `SYNTHESIS_MEMO.md` is present and coherent.
- The reviewer spot-checks 5 quotes against their claimed transcript source
  (by asking the librarian to produce the source excerpt). No fabricated quotes.
- Counter-cases are not suppressed — reviewer checks that the packages
  acknowledge disconfirming evidence.

Reviewer gives explicit APPROVE or BLOCK. Fabricated quotes are a hard stop
and must be reported to the researcher. Do not begin drafting until this
gate passes.

### G8 — Section Drafts Quality Gate

After all section drafts are complete (Method, Findings×3, Discussion,
Conclusion, Introduction, Abstract), before full paper assembly, the reviewer
performs a compliance pass against the writing template:

**Must check per section:**
- Method: 5 key design decisions documented; participant count stated; data
  volume table present; anonymisation statement included.
- Each Findings section: ends with numbered summary ("To summarise, N key
  findings emerge from this section. Firstly... Secondly...").
- All quotes: participant code + role descriptor + interview type present.
- Quotes are verbatim (hesitations preserved where analytically significant).
- Discussion: N sub-sections matching themes; each opens with conceptual
  claim before evidence; word count within 15% of target.
- Conclusion: opens with concrete analogy or image (not abstract restatement);
  names central theoretical contribution; ≥4 limitations; 3 specific future
  research directions named.
- Introduction: Para 1 opens with phenomenon (no gap language); Para 4
  empirical anchor present; closes with road map.
- Abstract: four-field format (Purpose / Design / Findings / Originality).

Reviewer gives section-by-section verdict. Any BLOCK per section reopens
that section for revision. Reviewer must approve all sections before full
paper assembly (Stage 9) begins.

### G9 — Template Compliance Final Check

After full paper assembly (Stage 9), the orchestrator runs the complete
Template Compliance Checklist from the SOP:

- [ ] Abstract: 4-field format (Purpose / Design / Findings / Originality)
- [ ] Introduction Para 1: opens with phenomenon — no gap language
- [ ] Introduction Para 4: empirical anchor (method, sample, period, activity)
- [ ] Introduction Para 8: road map — one sentence per section
- [ ] Each Findings section: numbered summary ("To summarise, N key findings...")
- [ ] Discussion: N sub-sections, each opens with conceptual claim
- [ ] Conclusion: opens with concrete analogy or image
- [ ] Conclusion: central theoretical contribution named
- [ ] Conclusion: ≥4 limitations stated
- [ ] Conclusion: 3 specific future research directions named
- [ ] All quotes: verified against transcripts, participant code attached
- [ ] All participant references: anonymised (code + role descriptor only)

Any unchecked item blocks closeout. The orchestrator fixes or instructs the
librarian to fix before the final reviewer review.

### G10 — Final Reviewer Clearance

The reviewer independently reads the full assembled draft and confirms:

- All sections are present and in the correct assembly order.
- All G8 section-level requirements are met in the assembled version.
- No real participant names or identifying details appear anywhere.
- The central theoretical contribution is clearly named and supported.
- The paper argument flows coherently from Introduction through Conclusion.
- No placeholder text remains (e.g. "[FIGURE]", "[citation needed]",
  "[TBC]") except the `*[To be compiled.]*` references placeholder.
- Word counts are within the template targets.

Reviewer gives explicit no-blocker verdict. Any blocker reopens the loop.
The references placeholder is not a blocker — it is the expected handoff
point to Citalio (Service 20).

### G11 — Closeout

The loop closes only when:

- All 9 SOP stages are marked complete.
- All gates G1–G10 are recorded as passed.
- Full draft file exists at `drafts/PAPER_FULL_DRAFT_v[N].md`.
- Paper-specific SOP (e.g., `PAPER4_WRITING_SOP.md`) is written and up to date.
- Template compliance checklist is fully checked.
- Reviewer no-blocker verdict (G10) is recorded.
- Handoff note to Citalio is written: draft path, `.docx` export instruction,
  key citations list.
- No open reviewer blockers remain.

---

## Standard Taskboard

### Stage 1 — Research Idea
1. Intake research idea; confirm all 6 fields; create paper folder structure.
2. Orchestrator scope lock (G1): confirm idea, folder, anonymisation policy, writing template, exit condition.

### Stage 2 — Literature Review
3. Librarian drafts LR1 (theoretical framework), iterates to v2+.
4. Librarian drafts LR2 (empirical domain), iterates to v2+.
5. Librarian produces deep reading notes for 3–5 anchor papers.
6. Reviewer LR quality gate (G2): APPROVE or BLOCK both LRs.

### Stage 3 — Writing Template Adoption
7. Librarian performs deep reading of template paper; produces DeepReading_Gate and Writing_Template files.
8. Orchestrator writing template confirmed (G3).

### Stage 4 — Method Design
9. Librarian writes Method_Research_Notes; assigns anonymisation codes; drafts Method_v1.
10. Orchestrator method and anonymisation gate (G4).

### Stage 5 — Coding Plan + Pre-Coding Reflexivity
11. Librarian writes PreCoding_Reflection_Memo (before any analytical reading).
12. Librarian writes LDA Coding Plan (or equivalent).
13. Reviewer pre-coding independence gate (G5): APPROVE or BLOCK.

### Stage 6 — Qualitative Coding
14. Librarian writes per-participant analytical memos for all participants.
15. Librarian writes cross-case synthesis memo.

### Stage 6b — Inter-Coder Verification
16. Librarian prepares CODER_BRIEF; independent coder produces memos; librarian writes INTER_CODER_COMPARISON.

### Stage 6c — NVivo Coding Package
17. Librarian produces NVivo package: CODEBOOK, SOURCE_LIST, THEMES_OVERVIEW, INTERCODER_SUMMARY, coded excerpts.
18. Reviewer coding completion and quality gate (G6): APPROVE or BLOCK.

### Stage 7 — Evidence Packaging
19. Librarian writes SYNTHESIS_MEMO and THEME[N]_EVIDENCE_PACKAGE_v2 files for all themes.
20. Reviewer evidence package gate (G7): quote spot-check, APPROVE or BLOCK.

### Stage 8 — Section Drafting
21. Librarian drafts Method section (iterates to v4+).
22. Librarian drafts Findings Theme 1, 2, 3 sections (separately, then combined).
23. Librarian drafts Discussion section.
24. Librarian drafts Conclusion section.
25. Librarian drafts Introduction section (after Findings are known).
26. Librarian drafts Abstract (last of all).
27. Reviewer section drafts quality gate (G8): per-section compliance check.

### Stage 9 — Full Paper Assembly
28. Librarian assembles full paper draft in correct section order; records word count per section.
29. Orchestrator template compliance final check (G9): runs full checklist.
30. Fix any compliance gaps identified in G9.
31. Reviewer final full paper review (G10): no-blocker verdict.
32. Fix any reviewer blockers (G10); re-confirm passage.
33. Librarian writes paper-specific SOP; orchestrator writes Citalio handoff note.
34. Closeout (G11): all gates recorded, all artifacts present, loop closed.

---

## Report Schema

```text
report_type:         (ORCHESTRATOR_INTAKE | LIBRARIAN_STAGE | REVIEWER_GATE
                      | ORCHESTRATOR_GATE | ORCHESTRATOR_CLOSE)
loop_id:
role:
stage:               (1 | 2 | 3 | 4 | 5 | 6 | 6b | 6c | 7 | 8 | 9)
gate:                (G1 | G2 | G3 | G4 | G5 | G6 | G7 | G8 | G9 | G10 | G11)

# --- INTAKE (step-001 to step-002) ---
research_idea_fields_confirmed:   yes | no | partial
paper_folder_path:
theoretical_lens:
empirical_data_confirmed:         yes | no
participant_count_approx:
writing_template:
anonymisation_policy:
exit_condition:

# --- LIBRARIAN STAGE REPORT (each stage completion) ---
stage_completed:
files_produced:       [path, ...]
files_updated:        [path, ...]
word_count:           (for draft stages)
iteration_version:    (e.g., v2, v4)
gate_ready:           yes | no
known_gaps:

# --- REVIEWER GATE REPORT ---
gate_id:
items_checked:        [item, verdict, notes]
spot_check_quotes:    [quote_ref, transcript_verified: yes/no]
verdict:              APPROVE | BLOCK
blockers:             [blocker_id, description, required_fix]
non_blockers:         [note, ...]

# --- ORCHESTRATOR GATE REPORT ---
gate_id:
checklist_items:      [item, status: pass/fail]
action_taken:
next_step:

# --- CLOSEOUT ---
all_stages_complete:     yes | no
all_gates_passed:        yes | no
full_draft_path:
full_draft_word_count:
paper_sop_path:
citalio_handoff_note_path:
open_blockers:
references_placeholder:  yes | no  (expected: yes — Citalio handles this)
```

---

## Handoff Outputs

When the loop closes, the following must exist:

| Artifact | Path | Purpose |
|---|---|---|
| Full draft (markdown) | `drafts/PAPER_FULL_DRAFT_v[N].md` | Source of truth |
| Paper-specific SOP | `[paperX_folder]/PAPER[N]_WRITING_SOP.md` | Pipeline record |
| Citalio handoff note | `drafts/CITALIO_HANDOFF.md` | Instructions for next service |

Citalio (Service 20) requires the draft to be exported to `.docx` before
processing. The Citalio handoff note must document this prerequisite.

---

## Anti-Patterns

- Beginning LR drafting before G1 scope lock is confirmed.
- Using an incomplete Research Idea.md as the anchor — if fields are missing,
  halt and ask the researcher.
- Writing the Pre-Coding Reflection Memo after analytical reading has begun.
- Allowing the independent coder to access the primary coder's memos before
  completing their own memos (voids the inter-coder independence claim).
- Using unverified quotes in Findings — every quote must trace to a transcript.
- Naming real participants instead of participant codes in any output file.
- Drafting the Introduction before Findings are substantially complete.
- Drafting the Abstract before the Introduction is complete.
- Assembling the full paper before the reviewer has cleared all sections (G8).
- Closing the loop before the Citalio handoff note is written — the
  references placeholder is a known open item, not an error.
- Treating word count targets as optional — deviations >20% require
  orchestrator decision before closeout.
- Skipping the pre-coding memo gate because coding "looks fine" — the gate
  checks the existence and credibility of the memo, not the coding quality.

---

## Word Count Targets (Gaudy 2023 Template)

| Section | Target |
|---|---|
| Abstract | ~250 words |
| Introduction | ~1,300 words |
| Method | ~1,600 words |
| Findings (total across all themes) | ~3,800–6,000 words |
| Discussion | ~2,200 words |
| Conclusion | ~500 words |
| **Total** | **~10,000–12,000 words** |

Deviations beyond ±20% of target must be flagged in the reviewer's G8 report
and resolved before assembly.

---

## Reference Case

- **Paper 4** — Mirroring Professional Identity: Lacanian Becomingness in
  Australian Carbon Emission Auditing
- Folder: `0404_paper4_Identity/`
- All 9 stages completed; Gaudy 2023 template applied; 17 participants;
  v3 full draft at ~12,791 words; pending Citalio handoff.
- Paper-specific SOP: `0404_paper4_Identity/PAPER4_WRITING_SOP.md`
- Master SOP: `00_main_library/SOP__Paper_Drafting_Service__v1.md`
- Service 22 entry: `00_main_library/LIBRARIAN_HANDBOOK.md`
