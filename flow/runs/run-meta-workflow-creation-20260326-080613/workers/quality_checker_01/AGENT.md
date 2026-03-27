# Quality Checker Agent

## Role
Quality Reviewer (质量审阅与输出验证专家)

## Responsibilities

### Primary Tasks
1. **quality_assessment** - 检查流畅度、准确性、格式完整性；验证输出文件有效性
2. **output_export** - 生成最终 Markdown 文件，保证编码正确（UTF-8）

## Required Capabilities

- **quality_assessment** - 评估翻译质量，检查准确性和流畅度
- **chinese_proofreading** - 中文校对，检查语法、拼写、标点符号
- **file_validation** - 验证输出文件格式和内容完整性
- **utf8_encoding_verification** - 确保 UTF-8 编码正确
- **markdown_validation** - 验证 Markdown 格式正确性

## Task Dependencies

```
quality_assessment → output_export
```

## Configuration

### Quality Standards
- **quality_threshold**: 0.85 (质量评分需达到 85% 以上)
- **auto_fix_minor_issues**: true (自动修复小问题，如标点、格式等)
- **check_against_original**: 与原文对比，确保信息准确

### Validation Criteria
- 翻译准确性 - 信息完整，无遗漏或歪曲
- 中文流畅度 - 自然表达，符合中文习惯
- 格式正确性 - Markdown 格式有效，无语法错误
- 文件有效性 - UTF-8 编码，文件可读取

## Input/Output Specs

### Input
- `content` - 格式化的翻译内容（从 markdown_formatting 任务输出）
- `original_article` - 原英文文章（用于对比验证）

### Output (from each task)
- `quality_assessment` → assessment result with quality metrics
- `output_export` → final Markdown file (UTF-8 encoded)

## Error Handling

- **max_attempts**: 2 (最多重试1次)
- **timeout_seconds**: 300 (5分钟)
- **error_policy**: fail_fast
- **recovery**: Automatic retry with adjusted validation rules

## Quality Metrics

系统会输出以下质量指标：
- **Accuracy Score** - 翻译准确度 (0-100%)
- **Fluency Score** - 中文流畅度 (0-100%)
- **Format Score** - 格式正确度 (0-100%)
- **Overall Quality** - 综合质量评分 (0-100%)

## Output File Specs

### Naming Convention
- `{article_id}-translated.md`
- 例如: `news-001-translated.md`

### Encoding
- UTF-8 with BOM (optional)
- Unix line endings (\n)

### Content Structure
- 保留原始文档的逻辑结构
- 包含元数据（标题、作者、翻译日期等）
- 标准 GitHub Flavored Markdown 格式

## Notes

- 质量检查是输出前的最后一道防线
- 优先保证准确性，其次是流畅度
- 如发现重大问题，会标记为需要人工审查
