# PowerPoint EXP

## Intent

Create polished PowerPoint decks for Barry with clean layouts, speaker notes,
slide rearranging, chart visuals, and slideshow validation.

## Context

Known to apply to Microsoft PowerPoint on Barry's HASHI Windows desktop with
`use_computer`, `windows_helper`, and optional PowerPoint object automation.

## Procedure

1. Avoid large pasted text blocks in placeholders.
2. Use a fixed layout system: title area, concise text area, visual area, and
   footer or section marker.
3. Keep slides text-light and business-focused.
4. Use object-level positioning for shapes, charts, and visual structure when
   quality matters.
5. Add speaker notes for every slide when the task includes presenting.
6. Rearrange slides intentionally and validate final order.
7. Open the deck in PowerPoint UI, start slideshow, advance slides, exit, and
   capture evidence.

## Evidence to keep

- final `.pptx`
- screenshot of editing view
- screenshot of slideshow start
- screenshot after advancing slides
- validation notes or report JSON

## Recovery

- If a UI-generated deck is ugly, replace placeholder-driven construction with
  template or object-level layout.
- If chart paste overlaps body text, allocate a dedicated visual region and use
  exact coordinates.
- If notes are missing, inspect the notes slide count and non-empty notes count.

## Scope limit

This EXP encodes Barry's quality preference and the current Office desktop
behavior. Do not generalize its visual style to unrelated users.
