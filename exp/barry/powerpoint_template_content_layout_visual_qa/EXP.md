# PowerPoint Template Content Layout Visual QA EXP

Status: candidate

This EXP captures the generic PowerPoint skill Barry wants to keep training:
take a user-selected PowerPoint template, compress an outline into low-density
slides, place content into the template without breaking the visual system, and
verify the result in real PowerPoint using screenshots before handover.

The core learning is that a PowerPoint file passing structural checks is not
enough. The deck is only acceptable after PowerPoint itself opens it and the
rendered slides are visually inspected.

## Current Learned Shape

The first training run used a blank content template and a 9-slide talk outline:
"The Uncomfortable Truths About AI". The successful route was:

- locate the saved template file from Windows recent documents
- inspect the `.pptx` package for slide count, slide size, layouts, and theme
- preserve the template's master, theme, background, and built-in layouts
- compress long outline content into short presentation-ready slide text
- generate versioned outputs instead of overwriting open files
- open the result in PowerPoint and capture screenshots for slide-level QA
- fix visual problems found only after rendering

## Production Rule

For this EXP, "done" means:

1. the output `.pptx` opens in desktop PowerPoint
2. slide count and main text match the requested outline
3. rendered slides have no text overlap, off-slide overflow, or unreadable text
4. text does not sit over bright or busy background regions unless contrast is
   clearly acceptable
5. the final filename/version is explicitly identified for Barry

## Status

Candidate only. The workflow has been trained once on a generic dramatic talk
deck, but it still needs more examples across:

- corporate templates with many placeholders
- image-heavy templates where picture placeholders must be preserved
- academic talk templates with tables and references
- automatic PDF/PNG export and batch visual comparison
- speaker notes generation

Do not stabilise this EXP until it has passed several more real template-fill
tasks with screenshot or export-based QA.
