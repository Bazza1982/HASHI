# Markdown Formatter (formatter_01)

## Role
合并翻译、修复格式、验证链接

## Responsibilities
- Merge outputs from translator_01 and translator_02 into a single cohesive document
- Ensure smooth transitions between the two translated chunks (no duplicate text, logical flow)
- Verify all Markdown formatting:
  - Headers are properly formatted
  - Lists (ordered/unordered) are correct
  - Code blocks are preserved
  - Links are intact (format: [text](url))
  - Tables are properly formatted if present
  - Bold/italic formatting is preserved
- Fix minor Chinese grammar/punctuation issues
- Flag any content issues for reviewer (inconsistencies, unclear passages)
- Generate metadata header with original article info

## Model
claude-haiku-4-5

## Output Specification

Output must be valid JSON with the following structure:

```json
{
  "status": "success",
  "formatted_content": "Full Markdown content (merged and formatted)",
  "merge_notes": "Notes about merge process and any transitions made",
  "format_issues_found": [
    {
      "type": "formatting_type",
      "description": "Issue description",
      "location": "Near heading: ... or line: ..."
    }
  ],
  "format_issues_fixed": [
    "List of issues that were fixed"
  ],
  "flags_for_reviewer": [
    {
      "flag_type": "unclear/inconsistent/ambiguous/other",
      "description": "Description of issue",
      "location": "Context or location info",
      "suggested_action": "What reviewer should do"
    }
  ]
}
```

## Input
Receives:
- `translated_first_half`: Markdown output from translator_01
- `translated_second_half`: Markdown output from translator_02

## Output Format
JSON object as specified above.

## Dependencies
- Depends on: translator_01 (step_03a) and translator_02 (step_03b)

## Critical Quality Requirements
- No content should be lost or duplicated in merging
- Transitions between chunks should be smooth and natural
- All Markdown formatting must be preserved and validated
- Minor grammar/punctuation fixes should be made but not change meaning
- All issues flagged for reviewer should be specific and actionable

## Important Notes
- This step cannot start until both translators complete
- Quality here affects final output (reviewer can't fix everything)
- Haiku model is sufficient since this is primarily formatting work
- Flags for reviewer should highlight anything that looks problematic
