# Deep Reading Gate EXP

## Intent

Before generating a Word writing template from a published journal article PDF,
the agent must first prove that it understands the article's structure,
argument logic, and transferable writing moves.

No Word output should be created until this gate passes.

## Required reading outputs

The agent must produce reading notes with these fields:

```text
article_identity:
  title:
  journal/style:
  article_type:
  source_file:

core_argument:
  one_sentence_summary:
  problem_addressed:
  contribution_claim:

genre_and_structure:
  article_genre:
  section_sequence:
  why_this_sequence_works:

section_logic:
  - section:
    purpose:
    paragraph_count_estimate:
    internal_progression:
    transferable_moves:
    non_transferable_content:

paragraph_role_map:
  - section:
    paragraph_or_cluster:
    role:
    what_it does_for_the_argument:
    reusable_instruction:

evidence_devices:
  tables:
  figures:
  appendices:
  what_each_device_does:

language_moves:
  gap_building:
  theory_positioning:
  method_justification:
  synthesis_language:
  contribution_language:
  limitation_language:

transferable_structure:
  what_can_become_template:
  what_must_be_replaced_for_new_topic:

quality_risks:
  possible_misreadings:
  unclear_sections:
  checks_needed_before_template:
```

## Pass criteria

The gate passes only if the notes:

- identify the article type and journal/style context
- explain why the section sequence works
- map each major section to its argument function
- describe paragraph or paragraph-cluster roles
- separate transferable structure from article-specific content
- identify the function of tables, figures, and appendices
- extract language moves without copying source wording
- give enough guidance to write a different paper in the same structural style

## Fail criteria

The gate fails if the notes:

- merely summarise the article
- copy source sentences into the template
- skip paragraph-level roles
- treat content findings as reusable structure
- ignore tables/figures/appendices
- cannot explain how the structure maps to a new topic
- jump directly to Word construction

## After the gate passes

Only after passing this gate should the agent create:

- a Markdown writing template
- a Word `.docx` template
- section placeholders
- paragraph-level writing prompts
- table/figure placeholder instructions
- validation notes
