# Training Run: AI Truths Template Fill

Date: 2026-05-05

Status: completed as candidate training evidence

## Goal

Train a generic PowerPoint workflow for taking a user-selected blank content
template and a supplied talk outline, then producing a low-density, high-impact
deck that Barry can later add pictures to.

## Input

- Template: `C:\Users\Print\OneDrive - The University Of Newcastle\ai\Powerpoint\Presentation1.pptx`
- Outline: "The Uncomfortable Truths About AI"
- Requested output: 9 slides, about 5 minutes, all English, layout/content only

## Accepted Output

- Final checked deck: `C:\Users\Print\OneDrive - The University Of Newcastle\ai\Powerpoint\The Uncomfortable Truths About AI_v5.pptx`

## What Worked

- Windows Recent documents quickly located the saved template.
- Copying the locked template to a temporary path allowed package inspection.
- OpenXML inspection identified slide size, layouts, and theme.
- `python-pptx` could build a versioned editable deck while preserving the
  template's master/theme/background.
- Screenshot QA in real PowerPoint caught issues that parser checks missed.

## Version History

- `v1`: rejected. Parser checks passed, but desktop PowerPoint could not read
  the file.
- `v2`: opened successfully, establishing that preserving existing template
  slides avoided the package problem.
- `v3`: rejected. Quote slide overflowed horizontally.
- `v4`: improved quote slide, but Real Risk slide body text sat on a bright
  background region.
- `v5`: accepted. Opened in desktop PowerPoint and screenshot-checked across
  the main slide set.

## Visual QA Evidence

Screenshots were taken during the run for slides 1, 2, 3, 4, 5, 6, 7, 8, and
9. The final accepted version was checked after the quote overflow and contrast
problems were fixed.

## Candidate Lessons

- A `.pptx` opening in `python-pptx` is not enough.
- Desktop PowerPoint rendering is the authority for layout acceptance.
- Placeholder indexes and placeholder types vary by template and must be
  inspected.
- Use explicit text boxes when placeholders are missing or unsuitable.
- Long quotes need manual line breaks.
- Bright template graphics define unsafe text zones.
- Versioned outputs are safer than overwriting an open Office file.
