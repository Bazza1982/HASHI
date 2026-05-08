# PowerPoint Validators

Use these checks before handing a generated deck to Barry.

## Structure Checks

- Deck includes cover, monthly update divider, key developments, timeline,
  pipeline, risks/forward plan, related business, Q&A, and upcoming meetings.
- Slide count is close to the source unless Barry asks for a shorter or longer
  version.
- Dates and reporting period are internally consistent.

## Visual Checks

- Export to PDF and inspect slide thumbnails.
- Headings are readable at presentation distance.
- Tables are not cramped.
- Timeline slides do not overload a single slide.
- The pipeline diagram is visually clear and not decorative filler.

## Content Checks

- Key developments are dated and action-oriented.
- Risks include forward actions, not just labels.
- Related business is separated from core project update.
- Upcoming meetings are formatted consistently.
- For rebuild training, run text parity checks against the source deck. Every
  source text shape must match the rebuilt text shape, including small section
  numbers and dates.

## EXP Checks

- The generated deck should feel like Barry's MoH update style, not a generic
  consulting deck.
- Any uncertain project facts must be marked as placeholders rather than
  invented.
- Do not rely only on image-difference thresholds. Small missing text such as
  `03` can pass visual thresholds because it occupies a tiny area of the slide.
