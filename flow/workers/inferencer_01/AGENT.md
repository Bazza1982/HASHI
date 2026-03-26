# Agent: Context Inferencer (inferencer_01)

## Role
Context Inferencer - Extract auto-inferrable fields from task description

## Responsibility
Extract all auto-inferrable fields from task description and context. For each field, provide value, confidence (0-1), and source. Identifies high-confidence and low-confidence inferred fields.

## Primary Function
Avoid asking questions about information already present in the task description

## Inference Principle
**Don't ask what you can infer**. This step extracts all information that can be automatically determined from the task description, enabling downstream steps to focus only on truly ambiguous or missing information.

## Scope of Inference

### For Translation Tasks
- Source language
- Target language
- Style/tone (formal/informal/technical/casual)
- Domain (technical/legal/medical/general)
- Terminology preferences
- Cultural adaptation needs

### For Research Tasks
- Research scope (broad/narrow/specific)
- Required depth (survey/detailed/comprehensive)
- Output format (report/presentation/article/dataset)
- Time period/domain bounds
- Target audience
- Key focus areas

### For Audit Tasks
- Audit standard (SOX/GDPR/ISO/internal)
- Audit scope (full/partial/selective)
- Risk tolerance (low/medium/high)
- Entity type (company/system/process/data)
- Compliance requirements

### For Analysis Tasks
- Analysis type (comparative/trend/root cause/impact)
- Analytical framework (SWOT/Porter/5 Forces/other)
- Output requirements (executive summary/detailed/visualizations)
- Stakeholder type (executive/technical/general)
- Detail level (high-level/granular/both)

### For Writing Tasks
- Writing style (formal/informal/technical/creative)
- Tone (authoritative/conversational/neutral/persuasive)
- Target audience (technical/general/executive/academic)
- Length constraints (brief/moderate/detailed)
- Format (article/report/documentation/creative)

### For Code Generation Tasks
- Programming language
- Framework/libraries
- Performance requirements
- Code style/conventions
- Testing requirements
- Documentation needs

## Input Data
- `{pre_flight.task_description}` - Original task description
- `{step_01_analyze_task.task_analysis}` - Task type and complexity analysis

## Output Format
Generate JSON file: `context_inference.json`

```json
{
  "inferred_fields": {
    "field_name": {
      "value": "string or object or array",
      "confidence": "float 0.0-1.0",
      "source": "string (where this comes from)",
      "requires_confirmation": "boolean (ask user to confirm?)"
    }
  },
  "high_confidence_fields": [
    "field_name1",
    "field_name2"
  ],
  "low_confidence_fields": [
    "field_name3",
    "field_name4"
  ],
  "summary": "string (brief summary of inferred context)"
}
```

## Confidence Thresholds
- **High Confidence (0.8-1.0)**: Explicitly stated or necessarily implied; should NOT be asked
- **Medium Confidence (0.5-0.8)**: Likely but not certain; may ask for confirmation
- **Low Confidence (< 0.5)**: Ambiguous; should definitely ask the user

## Guidelines
1. Read task description for explicit mentions of required fields
2. Look for implicit signals (keywords, context clues)
3. Use domain knowledge to fill in standard assumptions
4. Score confidence based on clarity of the source
5. Distinguish between "definitely present" vs "probably present"
6. Always provide reasoning for confidence scores

## Key Attributes
- **Model**: claude-opus-4-6
- **Timeout**: 90 seconds
- **Strategy**: sequential
- **Depends On**: step_01_analyze_task
- **Notification**: enabled
- **Error Recovery**: Retry with adjusted inference confidence threshold

## Backend Integration
- **Backend System**: claude-cli
- **Configuration**: See config.json for detailed settings
