# Extract Writing Template EXP

## Intent

Turn a real journal article or draft into a reusable writing template that
captures structure and rhetorical function, not source content.

## Procedure

0. Run the deep reading gate. Do not create Word output until the gate passes.
1. Identify the article genre and journal context.
2. Extract the section hierarchy.
3. Count paragraphs per section and subsection.
4. Assign each paragraph a rhetorical role.
5. Identify repeated language moves:
   - field positioning
   - literature gap
   - problem significance
   - theoretical lens
   - research questions
   - method justification
   - evidence synthesis
   - contribution framing
   - limitation and future research
6. Convert the structure into a fillable template.
7. Remove source-specific claims, data, citations, and findings.
8. Add guidance for paragraph length, voice, and transitions.
9. Validate that the template contains roles and placeholders rather than
   copied content.

## Output standard

The output should contain:

- article type
- section map
- paragraph-role map
- fillable writing template
- language-style guide
- do/don't notes
- validation notes

For Word output, the document should contain:

- clear Word headings
- fillable placeholder blocks
- section and paragraph-cluster tables
- quality checklist
- no copied source content

## Required precondition

The generated template must be based on reading notes that pass
`deep_reading_gate.exp.md`. If the notes are shallow, incomplete, or mostly a
summary, stop and improve the notes before writing the template.

## Failure modes

- Summarising the article instead of extracting a template.
- Copying source phrasing into the template.
- Over-generalising a journal-specific structure.
- Ignoring paragraph-level function.
- Treating visual formatting as writing structure.
