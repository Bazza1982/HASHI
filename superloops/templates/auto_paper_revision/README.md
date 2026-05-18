# Auto Paper Revision Superloop Template

## Purpose

Run a fully automated academic paper revision loop from an initial partial,
low-quality, fragmented, or outdated manuscript through redevelopment,
section rebuilding, evidence integration, and master-draft assembly to a
stable full draft ready for Citalio and final editing.

This template implements the Paper Revision Service SOP
(`00_main_library/SOP__Paper_Revision_Service__v1.md`) end-to-end. It is for
papers that already exist in draft form but are not yet argumentatively or
structurally stable. It does **not** include reference injection/repair
(Citalio) or precision `.docx` execution work (Paper Editing Service).

The loop does not close when the librarian says the paper is improved. It
closes only after the reviewer independently confirms that the revised paper
has a coherent governing argument, evidence-led findings, literature-engaged
discussion, clean conclusion logic, and one integrated master draft with no
major structural blockers remaining.

---

## Required Roles

- `orchestrator`: Owns scope lock, revision diagnosis, gate decisions,
  handoff boundaries, and final acceptance. Protects against drift into
  random patching. Must confirm that revision, not drafting or editing, is
  the correct service mode.
- `librarian`: Research and writing agent. Owns material audit, argument
  reframing, section redevelopment, literature/evidence integration, master
  draft assembly, status memo updates, and unresolved-issues logging. Does
  not self-accept any gate.
- `reviewer`: Independent quality agent. Reviews the diagnosis, the revised
  argument, the keep/cut/rewrite map, the rebuilt findings, the literature
  conversation in discussion, the integrated master draft, and the closeout
  handoff. Gives explicit `APPROVE` or `BLOCK` at each required gate.

---

## Inputs (required at loop start)

- **Paper folder**: absolute path to the paper project folder
- **Current draft set**: paths to the old combined draft and any section-level
  drafts already in play
- **Revision brief**: what is wrong with the current paper and what stronger
  paper is now sought
- **Best current evidence base**: coding outputs, evidence packs, synthesis
  memos, reviewed reports, interview/document paragraph packs, or equivalent
- **Literature base**: literature review sections plus paper-specific anchor
  literature notes that should govern the revised contribution
- **Target structure/template**: journal template or current governing paper
  structure, if one exists
- **Exit condition**: what counts as done, for example:
  `integrated master draft complete; argument stable; discussion literature-integrated; ready for Citalio and final editing`

---

## Service Fit Gate

Before major work begins, the orchestrator must confirm this is truly a
revision loop rather than a drafting or editing loop.

Use this template only when:

- a manuscript already exists;
- major sections are weak, outdated, fragmented, or misaligned;
- the task requires redevelopment, not simple polishing;
- the argument or findings architecture needs restructuring;
- the desired output is a new stable master draft.

Do **not** use this template when:

- no real draft exists yet -> use `auto_paper_draft`
- the manuscript is already substantively stable and only needs local edits
  or tracked changes -> use `auto_paper_edit`

---

## Non-Negotiable Gates

Before starting, read the shared orchestration guidance:

```text
superloops/config/orchestration_guidance.json
```

### G1 — Revision Mode Lock

Before any rewriting begins, the orchestrator must confirm:

1. A real existing manuscript or section draft set exists.
2. The revision brief names the actual problem:
   structure, argument, evidence integration, theory, section duplication,
   outdated sections, or unstable findings architecture.
3. The loop is correctly classified as `revision`, not `drafting` or `editing`.
4. The target paper folder and output master draft path are recorded.
5. Exit condition is recorded in loop state.

If the paper is too incomplete for revision, stop and redirect to
`auto_paper_draft`. If it is already stable enough for paragraph-level work,
stop and redirect to `auto_paper_edit`.

### G2 — Draft Diagnosis Gate

After the librarian audits the current draft set, the reviewer independently
checks that:

- the diagnosis identifies what is wrong with the current paper;
- usable versus obsolete sections are distinguished;
- the draft problem is not described only vaguely ("needs polish");
- the diagnosis is evidence-based rather than impressionistic.

Reviewer gives explicit `APPROVE` or `BLOCK`.

### G3 — Governing Argument Gate

After the librarian defines the revised governing argument, the orchestrator
confirms:

- the paper's central claim is now explicitly stated;
- the revised findings architecture is named;
- the intended discussion contribution is clear;
- old draft logic that no longer governs has been explicitly retired.

No section-level redevelopment proceeds without this gate passing.

### G4 — Material Audit and Revision Map Gate

After the librarian audits existing materials, the reviewer checks:

- every major section/component is classified as
  `retain`, `retain_with_light_revision`, `rewrite`, `replace`,
  `integrate_elsewhere`, or `hold_for_later`;
- evidence packs and memo sources are mapped to the revised argument;
- the plan does not preserve weak old sections merely for convenience;
- the revision map is sufficiently concrete to guide real writing work.

Reviewer gives explicit `APPROVE` or `BLOCK`.

### G5 — Findings Redevelopment Gate

After the librarian rebuilds the findings section, the reviewer independently
checks:

- each findings subsection has a clear analytical purpose;
- the evidence is paragraph/scenario-based, not just code-summary prose;
- quotations or document traces do analytical work;
- interviews, documents, and observations are integrated where relevant;
- findings are not overloaded with discussion-only conceptual claims.

Reviewer spot-checks source provenance on sampled empirical claims.
Reviewer gives explicit `APPROVE` or `BLOCK`.

### G6 — Discussion Literature-Integration Gate

After the librarian rebuilds the discussion, the reviewer independently checks:

- discussion does not merely restate findings;
- the literature is in active conversation with the findings;
- the paper's conceptual contribution is explicitly named;
- primary versus secondary contribution is distinguishable;
- the revised discussion is stronger than the previous draft logic.

Reviewer gives explicit `APPROVE` or `BLOCK`.

### G7 — Conclusion Closure Gate

After the librarian recasts the conclusion, the orchestrator confirms:

- the conclusion closes the paper rather than reopening discussion;
- duplicated framing from the discussion opening is removed;
- the central contribution is clearly restated in final form;
- limitations, implications, or forward path are proportionate and coherent.

### G8 — Master Draft Integration Gate

After the librarian assembles the new integrated master draft, the reviewer
checks:

- stable earlier sections were preserved only where justified;
- obsolete sections were actually replaced, not merely appended around;
- headings, numbering, and section order are coherent;
- transitions across old/new joins read as one paper;
- only one current master draft is designated as authoritative.

Reviewer gives explicit `APPROVE` or `BLOCK`.

### G9 — Stability Check

After the integrated master draft exists, the orchestrator confirms:

- no major repeated opening claims remain across findings/discussion/conclusion;
- no obvious placeholder clusters remain;
- section terminology is internally consistent;
- methods and literature framing still match the revised paper;
- the draft is stable enough for Citalio and final editing.

Decision must be one of:

- `stable_for_citation_and_editing`
- `needs_one_more_revision_pass`
- `returns_to_drafting`

### G10 — Final Reviewer Clearance

The reviewer independently reads the assembled master draft and confirms:

- the paper argument now flows coherently from start to finish;
- findings, discussion, and conclusion each do distinct work;
- there are no remaining major structural blockers;
- the draft is substantially better than the old draft set;
- unresolved issues, if any, are minor and explicitly logged for downstream work.

Reviewer gives `APPROVE` or `BLOCK`.

### G11 — Closeout

The loop closes only when:

- all revision stages are complete;
- gates G1–G10 are recorded as passed;
- the integrated master draft exists at the recorded target path;
- unresolved issues list is written;
- handoff note to Citalio and/or Paper Editing Service is written;
- no open reviewer blockers remain.

---

## Standard Taskboard

### Stage 1 — Diagnose the revision problem
1. Intake revision brief, current draft set, evidence base, literature base, and exit condition.
2. Orchestrator revision-mode lock (G1): confirm this is revision, not drafting or editing.
3. Librarian audits current draft set and writes draft diagnosis note.
4. Reviewer diagnosis gate (G2): approve or block the diagnosis.

### Stage 2 — Define the new governing argument
5. Librarian defines revised governing argument, findings architecture, and target contribution.
6. Orchestrator governing argument gate (G3).

### Stage 3 — Audit materials and build revision map
7. Librarian classifies all major materials: retain / light revise / rewrite / replace / integrate / hold.
8. Reviewer revision-map gate (G4).

### Stage 4 — Rebuild section architecture
9. Librarian writes revised section plan and redevelopment order.

### Stage 5 — Redevelop findings
10. Librarian rebuilds findings from the best current evidence base.
11. Reviewer findings redevelopment gate (G5).

### Stage 6 — Redevelop discussion
12. Librarian rebuilds discussion as literature conversation and contribution section.
13. Reviewer discussion gate (G6).

### Stage 7 — Recast conclusion
14. Librarian rewrites conclusion for closure and final contribution framing.
15. Orchestrator conclusion closure gate (G7).

### Stage 8 — Integrate master draft
16. Librarian merges stable retained sections plus rebuilt sections into one integrated master draft.
17. Reviewer master-draft integration gate (G8).

### Stage 9 — Stability and closeout
18. Orchestrator stability check (G9): stable for citation/editing, one more pass, or return to drafting.
19. Reviewer final clearance (G10).
20. Librarian writes handoff note and unresolved issues list.
21. Orchestrator closeout (G11).

---

## Report Schema

```text
report_type:            (ORCHESTRATOR_INTAKE | LIBRARIAN_DIAGNOSIS |
                         LIBRARIAN_REVISION_MAP | LIBRARIAN_SECTION_REBUILD |
                         REVIEWER_GATE | ORCHESTRATOR_GATE | ORCHESTRATOR_CLOSE)
loop_id:
role:
paper_folder:
current_draft_paths:
master_draft_target_path:

# --- INTAKE ---
revision_brief:
evidence_base_paths:
literature_base_paths:
exit_condition:
service_mode_confirmed: revision | drafting_redirect | editing_redirect

# --- DIAGNOSIS ---
current_draft_problem_summary:
usable_sections:
obsolete_sections:
key_structural_problems:
key_argument_problems:

# --- ARGUMENT / MAP ---
revised_governing_argument:
revised_findings_architecture:
primary_contribution:
secondary_contributions:
material_classification_summary:
retain_paths:
rewrite_paths:
replace_paths:
integrate_elsewhere_paths:
hold_for_later_paths:

# --- SECTION WORK ---
section_name:
section_output_path:
section_word_count:
evidence_sources_used:
literature_sources_used:
major_claims_rebuilt:
open_questions:

# --- GATES ---
gate_id:
gate_verdict:           APPROVE | BLOCK
blockers:
required_fixes:

# --- INTEGRATION / CLOSEOUT ---
integrated_master_draft_path:
transitions_checked:
placeholder_audit_result:
stability_decision:
handoff_target:         Citalio | Paper Editing Service | both
unresolved_issues_path:
closeout_verdict:
```

---

## Closeout Standard

This loop is successful only if it produces a paper that has clearly moved
from:

- partial or low-quality draft
- fragmented or outdated logic
- unstable section architecture

to:

- one explicit governing argument
- evidence-led findings
- literature-engaged discussion
- conclusion with real closure
- one integrated master draft ready for downstream citation and editing

If the result is still a pile of section drafts, the loop has not succeeded.

