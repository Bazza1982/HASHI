# Integrated Office Workflows EXP

## Intent

Coordinate Excel, Word, PowerPoint, and PDF output as one workflow with reliable
evidence and validation.

## Context

Known to apply to Barry's HASHI Windows desktop where `use_computer` validates
real UI behavior and helper/object automation handles precise document creation.

## Procedure

1. Build source analysis in Excel first.
2. Validate formulas, filters, conditional formatting, and charts.
3. Move charts or findings into Word or PowerPoint through clipboard or object
   automation.
4. Save native Office files before exporting derived formats.
5. Use application-native export flows for PDF.
6. Capture UI evidence from the final application, not only filesystem checks.

## Evidence to keep

- source `.xlsx`
- destination `.docx` or `.pptx`
- exported `.pdf` when requested
- screenshots from each application
- validation report with structural checks

## Recovery

- If cross-app clipboard content fails, save the visual as an image or use
  object-level insertion.
- If PDF export creates a wrong extension, use the application export command.
- If a final file opens but looks wrong, prefer structural regeneration from
  templates over manual patching in the UI.

## Scope limit

This workflow is optimized for Barry's Office tasks and should be revalidated
before being applied to another desktop environment.
