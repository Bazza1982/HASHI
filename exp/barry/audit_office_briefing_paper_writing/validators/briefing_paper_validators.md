# Briefing Paper Validators

Use these checks before handing a briefing paper artefact to Barry.

## Structure Checks

- Front briefing metadata table is present.
- Purpose section is present and concise.
- Proposal/rationale section explains what is proposed and why it matters.
- Feedback section uses clear bullets when applicable.
- In-scope and out-of-scope are separated.
- Strategic alignment is represented in a table when required.
- Governance/team section identifies sponsors, internal team, contributors, and
  external advisors where applicable.

## Word Formatting Checks

- Export both source and rebuilt document to PDF.
- Compare page count and page flow.
- Render each PDF page to image and compare visually when reproducing a sample.
- Check the front metadata box against the source: it should use the learned
  rule-line layout, not a generic visible grid table.
- If Barry provides a manually adjusted screenshot, treat screenshot page flow
  and density as the target, not only the original sample document.
- When metadata fields are removed, compact the metadata block and merge broad
  value fields so remaining values do not wrap awkwardly.
- Check heading colours and hierarchy.
- Check heading colour consistency against the visual target. Do not accept
  different red tones simply because Word uses different heading style names.
- When a screenshot target is available, sample or visually compare the rendered
  heading/subheading colours and record the intended colour token.
- Check bullet indentation and spacing.
- Check Word numbering context, not only visible bullet text.
- Check table width, columns, borders, shading, and text wrapping.
- Check fonts and sizes against source styles.
- Check that no direct font overrides have been introduced where the learned
  paragraph styles should control the appearance.

## Content Checks

- Generated text must follow the source's briefing style, not its topic.
- No long source sentences should be copied.
- Unknown facts must be placeholders or flagged assumptions.

## Final Handover Checks

- Visual page comparison completed.
- Text/paragraph structure parity completed where rebuilding a sample.
- Table count, table content, and table role checks completed.
- Media/drawing object parity completed where rebuilding a sample.
- Style usage parity completed where rebuilding a sample.
- Human-readable PDF preview created.
