# Template Content Layout Visual QA Playbook

Use this playbook when Barry provides a PowerPoint template and an outline, and
asks for layout/content work while images will be added separately.

## Workflow

1. Locate the saved `.pptx` template.
2. If the deck is open and locked, copy it to a temporary read-only analysis
   path before inspecting.
3. Inspect the PowerPoint package or use Office automation to identify:
   - slide size
   - existing slide count
   - available slide layouts
   - placeholder types and indexes
   - theme colours and background treatment
4. Compress the outline into slide-ready text:
   - one core message per slide
   - short lines over paragraphs
   - quotes broken manually into safe line lengths
   - avoid dense blocks unless Barry explicitly wants detail
5. Build a versioned output deck from the template.
6. Preserve template master/theme/backgrounds wherever possible.
7. Use built-in layouts where they work.
8. Use explicit text boxes when a slide has no suitable title/body placeholder.
9. Never treat structural validation as final.
10. Open the output in desktop PowerPoint and screenshot-check representative
    or all slides.
11. Revise any slide with overlap, off-slide overflow, low contrast, or text
    sitting on visually busy background regions.
12. Hand over the final version only after visual QA.

## Layout Rules

- Prefer the user's template language over inventing a new design.
- Do not add pictures when Barry says pictures will be added separately.
- Do not fill picture placeholders with decorative stock art.
- Keep title slides dramatic but readable.
- Keep content slides low density for short talks.
- For quote slides, do not rely on automatic wrapping; insert manual line
  breaks for long quotes.
- For bright backgrounds or decorative waves, keep body text in high-contrast
  safe regions.
- If PowerPoint is open with the output file, write a new version instead of
  overwriting the locked file.

## Required Handover

The handover must identify:

- final `.pptx` path
- whether it was opened in desktop PowerPoint
- which slides were visually checked
- any remaining risks, such as no PDF export or no full-screen slideshow check

## Candidate Gaps

This EXP still needs training on automatic export to PDF/PNG, reliable batch
slide rendering, richer image-placeholder preservation, and speaker notes.
