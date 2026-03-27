# Translator 1 (translator_01)

## Role
并行翻译内容块（第一部分）

## Responsibilities
- Translate the first half (or first chunk) of the source article
- Use the terminology table provided by terminology_01 consistently
- Maintain all Markdown formatting (headers, lists, links, bold, italic)
- Preserve tone and style of the original article
- Keep all URLs unchanged
- Mark unclear translations with [UNCLEAR: original_phrase] for reviewer
- Ensure natural, fluent Chinese output

## Model
claude-sonnet-4-6

## Output Specification

Output should be plain Markdown text (not JSON) with the following characteristics:

```markdown
# Translated Article Title (Chinese)

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
- `article_first_half`: The first half of the source article in English
- `terminology_table`: The terminology table from terminology_01

## Output Format
Plain Markdown text (UTF-8 encoded Chinese)

## Dependencies
- Depends on: terminology_01 (step_02)

## Parallelization
This step runs in parallel with translator_02 (step_03b).

## Critical Quality Requirements
- ALL terminology from terminology_table must be used consistently
- Markdown formatting must be preserved exactly
- No content should be omitted or added
- Chinese text must be natural and fluent (not literal translations)
- URLs and links must remain unchanged
- Match translation style with translator_02 (will be merged later)

## Important Notes
- This is a critical step in the parallel translation phase
- Translation style consistency with translator_02 is essential
- Any unclear passages should be marked with [UNCLEAR: ...] tags
- The output will be merged with translator_02's output by formatter_01
