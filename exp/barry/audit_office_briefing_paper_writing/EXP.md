# Audit Office Briefing Paper Writing EXP

Status: training

This EXP trains agents to create Audit Office briefing papers in Barry's
expected Word format and language style.

The source sample is used to learn:

- document structure
- fonts and paragraph styles
- heading colours and hierarchy
- briefing table layout
- strategic alignment table style
- concise executive language
- audit-office framing and risk-aware wording

The source sample is not used as reusable content.

## Initial Learned Shape

The sample paper is a 9-page Word briefing paper with:

- front briefing metadata table
- red `Purpose` heading
- purpose paragraph
- issue/proposal section explaining what is proposed and why it matters
- feedback section using first-level bullets
- scope section with first- and second-level bullets
- out-of-scope section
- strategic alignment section using tables
- governance and project team section

## Style Signals

Current extraction shows:

- 52 meaningful paragraphs
- 3 tables
- 2 media objects
- 7 paragraph styles in active use
- red heading style around `D64B46`
- dark red subheading style around `6D1C1A`
- heavy use of `Heading3`, `Bullet1stlevel`, `Bullet2ndlevel`, and
  `ListParagraph`

## Training Status

The source document has been copied, exported to PDF, rendered to page images,
and structurally extracted. The next step is blank/template-context rebuild
training, followed by visual and text/style parity checks.
