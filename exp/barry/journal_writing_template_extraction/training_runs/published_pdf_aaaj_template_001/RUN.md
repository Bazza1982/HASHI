# Published PDF to Writing Template Training Run 001

Status: v2 candidate-handover template generated; ready for Barry review.

## Correct source

The source is the published journal article PDF:

```text
training_materials/Current template - 2021 Unpacking dialogic accounting a systematic literature review.pdf
```

This source is a published AAAJ-style systematic literature review article.

## Relationship to Barry's draft

Barry's draft should not be treated as the training source. It is the result of
adapting the published PDF's writing structure to a different topic.

The relationship is:

```text
published AAAJ SLR PDF -> extracted writing structure -> Barry's NFIA draft
```

The link is structural, not visual:

- both use a structured AAAJ abstract
- both open with a broad field-level problem
- both move from fragmented literature to a theory-led review warrant
- both use research questions to connect mapping and conceptual synthesis
- both include a review design/methodology section
- both use tables/figures to make the corpus and synthesis transparent
- both end with discussion/conclusion that returns to theory and contribution

## Source macro-structure detected

From the PDF extraction:

1. Abstract
2. Introduction
3. Antecedents/theoretical background section
4. Review design and exploratory analyses
5. Narrative review and discussion of the contributions
6. Discussion and conclusions
7. References
8. Appendix

## Draft adaptation pattern

Barry's draft adapts that structure as:

1. Structured abstract
2. Introduction
3. Conceptual Foundations
4. Review Design and Methodology
5. Analysis and Thematic Synthesis
6. Discussion and Conclusion
7. Conclusion
8. References

## Training objective

Train an EXP that can take a published journal article PDF and generate a Word
writing template that captures:

- section architecture
- paragraph roles
- rough paragraph counts
- language moves
- contribution logic
- table/figure functions
- what each paragraph is doing

The template must not copy article content.

## Training stages

1. Extract PDF text and page structure.
2. Perform a deep reading pass.
3. Fill the deep reading notes template.
4. Pass the understanding quality gate.
5. Detect section headings and article genre.
6. Segment body text into sections and paragraphs.
7. Classify each paragraph by rhetorical role.
8. Convert section and paragraph roles into a fillable writing template.
9. Validate against source PDF and Barry's draft adaptation.
10. Ask Barry to confirm that the template captures the structure.
11. Stabilise the EXP only after approval and one successful transfer to a new
   article/PDF.

## Required outputs

- PDF structure seed JSON
- source-to-draft relationship map
- writing-template extraction playbook
- generated template from the published PDF
- validation report showing source content was not copied

## Current artifacts

- `state/published_pdf_structure_seed.json`
- `state/published_pdf_full_text.txt`
- `state/published_pdf_section_segments.json`
- `state/published_pdf_blocks_v2.json`
- `state/deep_reading_notes_template.md`
- `state/deep_reading_notes.md`
- `state/deep_reading_gate_report.json`
- `state/paragraph_role_map_v2.md`
- `state/published_pdf_template_validation_report.json`
- `state/word_template_export_log.json`
- `state/word_template_validation_report.json`
- `state/word_template_v2_export_log.json`
- `state/word_template_v2_validation_report.json`
- `state/mini_test_write_validation_report.json`
- `output/published_pdf_aaaj_slr_writing_template.md`
- `output/published_pdf_aaaj_slr_writing_template_v2.md`
- `output/published_pdf_aaaj_slr_word_writing_template.docx`
- `output/published_pdf_aaaj_slr_word_writing_template.pdf`
- `output/published_pdf_aaaj_slr_word_writing_template_v2.docx`
- `output/published_pdf_aaaj_slr_word_writing_template_v2.pdf`
- `output/template_v2_mini_test_write.md`

## Gate result

The deep reading gate passed. The reading notes identify:

- article identity
- core argument
- section sequence
- section logic
- paragraph-cluster roles
- evidence devices
- language moves
- transferable structure
- quality risks

## Template validation

The Markdown template was checked for copied long snippets from the PDF.

```text
copied_long_snippet_flag_count: 0
status: passed
```

## Word template validation

The Word template was generated only after the deep reading gate passed.

```text
heading_count: 13
table_count: 8
copied_long_snippet_flag_count: 0
status: passed
```

## V2 critical self-review response

The first Word template passed basic validation but was not strong enough for
handover because it was too compact and too close to a generic academic-writing
framework. V2 fixes that by adding a paragraph-level role map, a fuller
Markdown template, a six-page Word workbench, and a mini transfer test.

V2 validation:

```text
pages: 6
words: 2795
heading_count: 14
table_count: 8
copied_source_phrase_flags: 0
missing_required_units: 0
status: passed
```

Mini transfer test:

```text
test topic: AI assurance in public-sector accountability
introduction role checks: 8/8
topic_shift_confirmed: true
status: passed
```

## Handover decision

This run is now ready for Barry review as a candidate-stable EXP artifact. It
should not be marked stable globally until Barry confirms that the Word template
is intuitive for both human use and future AI-agent use.
