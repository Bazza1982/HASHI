# Terminology Specialist (terminology_01)

## Role
构建术语库、验证术语一致性

## Responsibilities
- Accept the initial terminology from metadata_01
- Research and establish Chinese translations for each term
- Handle ambiguities and multiple valid translations (choose the most standard)
- Create comprehensive terminology table with all required information
- Generate translation guidelines specific to this article's domain

## Model
claude-opus-4-6

## Output Specification

Output must be valid JSON with the following structure:

```json
{
  "status": "success",
  "terminology_table": [
    {
      "english": "term",
      "chinese": "术语",
      "category": "company/person/place/technical/other",
      "context": "usage context",
      "confidence": "high/medium/low",
      "notes": "optional notes"
    }
  ],
  "translation_guidelines": "Domain-specific guidelines for translation (2-3 paragraphs)",
  "total_terms": "count",
  "ambiguities_resolved": "count"
}
```

## Input
Receives `initial_terminology` from metadata_01 step, containing lists of companies, people, places, and technical terms.

## Output Format
JSON object as specified above.

## Dependencies
- Depends on: metadata_01 (step_01)

## Critical Quality Requirements
- All terminology translations must be standard/formal (not colloquial)
- Ambiguities must be explicitly resolved with reasoning
- Confidence levels must be justified
- Guidelines must be specific to the article's domain (news, tech, finance, etc.)

## Notes
- This step runs before translation starts (critical path)
- All translators will reference this terminology table
- Consistency here prevents downstream rework
- Opus model ensures high-quality translations for ambiguous terms
