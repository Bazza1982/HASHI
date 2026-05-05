# Barry PhD Paper AAAJ Style Validators

This validator file is a placeholder until style extraction is complete.

Use these initial checks when training the EXP:

- Confirm which draft version was used as the primary example.
- Confirm whether formatting comes from AAAJ template requirements or Barry's
  own writing habit.
- Confirm whether a style rule appears in multiple drafts.
- Confirm whether supervisor-preferred wording is explicit or inferred.
- Keep generated practice output separate from original training material.
- Ask Barry before promoting inferred rules to stable EXP.

## Baseline checks added from blank_rebuild_001

- Source and rebuilt PDFs should have matching page counts for clone-style
  baseline tests.
- Table count should match the source when exact reproduction is requested.
- Inline shape count should match the source when exact reproduction is
  requested.
- Section count should match the source when exact reproduction is requested.
- A run can fail layout validation even if tables, images, and sections match.
- A run can also fail exactness even when page count matches; compare rendered
  pages when the requirement is "looks exactly like the original on every page."
- For exact clone-style baselines, require every rendered page to match within
  the declared pixel threshold.

## Sakura/librarian citation workflow visual checks

- If the citation-repaired output is visually flat, check whether every
  paragraph has been left as `Normal` despite the manuscript structure being
  present in the text.
- Keep the accepted workflow artifact unchanged; make a separate editing copy
  before applying visual formatting.
- Reconstruct visible hierarchy for title, subtitle, Abstract, Keywords, section
  headings, captions, body paragraphs, and References.
- Check in desktop Word that in-text citations do not remain blue/underlined
  unless Barry explicitly wants clickable citation styling to remain visible.
- Export the visually edited copy to PDF and record page count.
