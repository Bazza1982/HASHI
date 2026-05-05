# PowerPoint Visual QA Validators

Use these validators before handing Barry a generated PowerPoint deck.

## Structural Checks

- The `.pptx` file exists and has non-trivial size.
- The deck opens with a parser such as `python-pptx`.
- Slide count matches the requested outline.
- Key slide titles and major body text are present.
- The generated file has no duplicate slide XML warnings from a delete-and-save
  workflow.

Structural checks are necessary but not sufficient.

## Desktop PowerPoint Checks

- Open the final candidate in desktop PowerPoint.
- Confirm PowerPoint does not show a "can't read" or repair dialog.
- Capture screenshots after opening.
- Visually inspect slides for:
  - text overlap
  - text outside slide bounds
  - text hidden by the PowerPoint UI because the slide itself has overflow
  - low contrast against bright or busy background regions
  - title truncation
  - body text too small for presentation delivery
  - broken bullets or unexpected wrapping

## Revision Rules

- If a rendered slide fails, create a new version and re-check.
- If a file is locked because it is open in PowerPoint, do not overwrite it.
- If quote text overflows, manually break the quote into short lines and reduce
  font size.
- If body text lands on a bright template image, move it into a darker safe
  region or reduce density.
- If a template placeholder is absent, add explicit text boxes with stable
  coordinates.

## Acceptance Standard

The deck is acceptable only when the final version opens in PowerPoint and all
material slides checked by screenshot are visually readable.
