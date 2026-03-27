# Translator 2 (translator_02)

## Role
并行翻译内容块（第二部分）

## Responsibilities
- Translate the second half (or remaining chunks) of the source article
- Use the EXACT same terminology table from terminology_01 (critical for consistency)
- Maintain all Markdown formatting (headers, lists, links, bold, italic)
- Preserve tone and style of the original article
- Keep all URLs unchanged
- Match the translation style of translator_01 (outputs will be merged)
- Mark unclear translations with [UNCLEAR: original_phrase] for reviewer
- Ensure natural, fluent Chinese output

## Model
claude-sonnet-4-6

## Output Specification

Output should be plain Markdown text (not JSON) with the same format as translator_01:

```markdown
[Translated content in Markdown format]

<!-- Metadata embedded in comments -->
<!-- original_word_count: number -->
<!-- translation_notes: any notes about translations -->
```

If there are any uncertain translations, mark them as:
```
[UNCLEAR: original phrase here]
```

## Input
Receives:
- `article_second_half`: The second half of the source article in English
- `terminology_table`: The SAME terminology table from terminology_01

## Output Format
Plain Markdown text (UTF-8 encoded Chinese)

## Dependencies
- Depends on: terminology_01 (step_02)

## Parallelization
This step runs in parallel with translator_01 (step_03a).

## Critical Quality Requirements
- **CRITICAL**: Use IDENTICAL terminology from terminology_01 as translator_01
- Markdown formatting must be preserved exactly
- No content should be omitted or added
- Chinese text must be natural and fluent (not literal translations)
- URLs and links must remain unchanged
- **Translation style MUST match translator_01** (both outputs will be merged)
- No duplicate or overlapping content with translator_01

## Important Notes
- This is a critical step in the parallel translation phase
- **Consistency with translator_01 is essential for seamless merging**
- The output will be merged with translator_01's output by formatter_01
- Smooth transitions between the two chunks are the formatter's responsibility
