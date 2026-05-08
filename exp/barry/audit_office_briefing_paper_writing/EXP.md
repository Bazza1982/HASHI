# Audit Office Briefing Paper Writing EXP

Status: stable

This EXP trains agents to create Audit Office briefing papers in Barry's
expected Word format and language style.

This EXP is stable for Barry's Audit Office briefing note drafts after:

- exact sample rebuild training
- a real unformatted input run
- Barry's manual screenshot review
- screenshot-target re-training
- failure-memory and validator updates

The source sample and Barry's approved screenshot target are used to learn:

- document structure
- fonts and paragraph styles
- heading colours and hierarchy
- briefing table layout
- strategic alignment table style
- compact metadata block behaviour when fields are removed
- screenshot-target page flow and density
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

Barry's screenshot-target run added the following stable formatting signals:

- use a compact four-field metadata block when unnecessary metadata fields are
  removed
- use horizontal rule lines in the metadata block, not a generic grid table
- merge broad metadata value fields so Subject and Prepared by do not wrap
  awkwardly
- maintain a two-page compact briefing-note flow when the content allows it
- keep legal/privacy content at the end of page 1 and lifecycle/risk/
  recommendation content on page 2 for the trained sample-input pattern
- keep heading/subheading colour visually consistent with the target screenshot,
  not merely with Word style names

## Training Status

The source document has been copied, exported to PDF, rendered to page images,
structurally extracted, and rebuilt in training.

The first blank Word rebuild failed because it lost bullet/indent/page-flow
fidelity. The successful route is a template-context rebuild: keep the learned
Word style context, clear the document body, and rebuild the briefing paper
inside that context.

The candidate rebuild passed:

- PDF visual parity: 9 pages vs 9 pages, pixel difference `0.0`
- paragraph parity: 52 vs 52
- table parity: pass
- media/drawing parity: pass
- style usage parity: pass

This EXP passed a supervised real briefing paper run and Barry approved
stabilising it after screenshot-target correction.

## Operating Rule

For this EXP, do not use Word's default blank document when exact Audit Office
briefing style is required. Use the learned style context as the starting shell,
then write new briefing content into an empty body. The reusable knowledge is
the structure, fonts, colours, bullet behaviour, tables, and briefing language,
not the source content.

For Barry's preferred briefing-note output, also treat his manually adjusted
screenshot as a visual target. The final output must pass a rendered PDF review
for metadata compactness, heading colour consistency, paragraph density, table
compactness, and page flow before handover.
