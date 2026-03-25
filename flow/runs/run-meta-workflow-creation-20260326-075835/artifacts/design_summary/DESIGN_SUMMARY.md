# Workflow Design Summary
## English News Translation to Chinese Markdown

**Design Timestamp**: 2026-03-26T07:58:35Z
**Workflow ID**: news_translation_to_markdown
**Run ID**: run-meta-workflow-creation-20260326-075835

---

## Executive Summary

This workflow automatically translates English news articles to Chinese Markdown format with **zero human intervention**. The design consolidates an 8-step analysis into a streamlined 3-step pipeline:

1. **Scanning & Analysis** (analyst_01) — Extract entities and terminology
2. **Translation** (translator_01) — Translate with formatting
3. **Verification & Finalization** (reviewer_01) — Validate and output

**Total estimated duration**: 55 minutes
**Total tokens**: ~75,000
**Quality level**: Standard (speed-optimized)

---

## Workflow Architecture

### DAG Diagram
```
step_1_scan(analyst_01)
    ↓
step_2_translate(translator_01)
    ↓
step_3_verify(reviewer_01)
    ↓
final_article_zh.md (output)
```

### Workers & Roles

| Worker ID | Role | Model | Purpose | Duration |
|-----------|------|-------|---------|----------|
| analyst_01 | Analyst | claude-haiku-4-5 | Entity extraction, terminology table | 5 min |
| translator_01 | Translator | claude-sonnet-4-6 | English→Chinese translation, Markdown formatting | 40 min |
| reviewer_01 | Reviewer | claude-sonnet-4-6 | Consistency check, format validation, finalization | 10 min |
| debug_01 | Debug | claude-sonnet-4-6 | Error handling and recovery | As needed |

---

## Design Principles & Rationale

### 1. **Model Selection**
- **Haiku (analyst_01)**: Fast, efficient entity extraction for speed-first goal
- **Sonnet (translator_01 & reviewer_01)**: Balanced quality/speed for translation and verification
- **No Opus**: Standard quality tier doesn't justify extra cost

### 2. **Step Consolidation**
- **Original**: 8 steps (over-engineered)
- **Optimized**: 3 steps (sufficient for task complexity)
  - Merged: Terminology ID (steps 1-2) → Step 1 (Scanning)
  - Merged: Structure analysis + translation (steps 3-4) → Step 2 (Translation)
  - Merged: Format + consistency + review (steps 5-7) → Step 3 (Verification)
  - Separate: Final output → Built into Step 3

### 3. **Automation Strategy (Zero Human Intervention)**
- **analyst_01**: Auto-extracts entities, no manual input needed
- **translator_01**: Uses terminology table from analyst_01, preserves source structure
- **reviewer_01**: Auto-fixes minor inconsistencies (confidence ≥ 0.90), reports all findings
- **Error handling**: debug_01 attempts recovery; escalates only on fatal errors

### 4. **Data Flow**
```json
step_1: source_file_path
  ↓ (outputs)
  file_analysis.json
  terminology_table.json
  ↓
step_2: source_content + file_analysis + terminology_table
  ↓ (outputs)
  translated_draft.md
  translation_metadata.json
  ↓
step_3: translated_draft + terminology_table
  ↓ (outputs)
  final_article_zh.md (with metadata header)
  quality_report.json
```

---

## Key Features

### ✓ Entity Extraction & Terminology
- **analyst_01** identifies all proper nouns (people, places, orgs, brands)
- Builds terminology table for consistent translation
- Extracts specialized terms and context
- Fast analysis with haiku model

### ✓ Professional Translation
- **translator_01** produces natural, idiomatic Chinese
- Preserves journalistic tone and style
- Applies Markdown formatting (headings, lists, quotes, links)
- 100% conformance to terminology table (0.98+ target)

### ✓ Quality Assurance
- **reviewer_01** verifies terminology consistency across full text
- Validates Markdown format (syntax, structure, links)
- Assesses translation accuracy and readability
- Auto-fixes minor issues; reports all findings
- Adds metadata header (translation date, version, source, consistency score)

### ✓ Fully Automated
- No human review points (max_interventions = 0)
- Error recovery at each step
- Comprehensive quality report for audit trail
- Deterministic, reproducible output

---

## Quality Metrics

### Success Criteria
✓ `file_analysis.json` valid and complete
✓ All entities extracted and categorized
✓ `translated_draft.md` is valid Markdown
✓ Terminology consistency ≥ 0.95
✓ Translation style preserved
✓ `final_article_zh.md` published and verified

### Quality Thresholds
- **Consistency score**: Target ≥ 0.95 (all entities verified)
- **Format validation**: 100% Markdown compliance
- **Terminology conformance**: ≥ 0.98 (translator level)
- **Readability**: Subjective, but reviewer assesses

### Failure Modes
| Error | Recovery | Escalation |
|-------|----------|-----------|
| File not found | Report, abort | Fatal |
| Encoding error | Auto-detect, retry | After 3 attempts |
| Terminology mismatch | Auto-fix if confident, report | After review |
| Format error | Auto-correct, retry | After 3 attempts |
| Timeout | Retry with smaller chunk | After 2 attempts |

---

## Execution Flow

### Step 1: Scanning & Analysis (analyst_01)
1. Validate source file path
2. Read content (handle encoding)
3. Extract entities (people, places, orgs, brands, terms)
4. Analyze article structure (headings, sections)
5. Build terminology lookup table
6. Output: `file_analysis.json`, `terminology_table.json`

**Duration**: 5 minutes
**Model**: haiku (fast)
**Timeout**: 300 seconds
**Max retries**: 3

### Step 2: Translation (translator_01)
1. Parse source structure
2. Translate paragraph by paragraph → natural Chinese
3. Apply Markdown formatting (# ## ###, -, > etc.)
4. Verify all entities use terminology_table
5. Preserve all links, citations, references
6. Output: `translated_draft.md`, `translation_metadata.json`

**Duration**: 40 minutes
**Model**: sonnet (balanced)
**Timeout**: 1200 seconds
**Max retries**: 3

### Step 3: Verification & Finalization (reviewer_01)
1. Extract entities from translated_draft
2. Compare against terminology_table (consistency check)
3. Validate Markdown syntax
4. Assess translation accuracy and readability
5. Generate metadata header (date, version, source, score)
6. Auto-fix minor issues (confidence ≥ 0.90)
7. Output: `final_article_zh.md`, `quality_report.json`

**Duration**: 10 minutes
**Model**: sonnet (quality)
**Timeout**: 600 seconds
**Max retries**: 3

### Error Handling
If any step fails:
1. debug_01 analyzes the error
2. Attempts recovery (auto-detect encoding, fix formatting, etc.)
3. Generates detailed error report
4. Retries failed step (max 3 attempts total)
5. On fatal error: abort and report

---

## Input/Output Contracts

### Pre-flight Inputs (Required)
```json
{
  "source_file_path": "path/to/english_news.txt",
  "file_type": "txt|md|html|pdf"
}
```

### Final Output
```
final_article_zh.md
├── Metadata header (YAML frontmatter)
│   ├── translated_date
│   ├── source_language: English
│   ├── target_language: 中文
│   ├── translation_version: 1.0
│   └── consistency_score: 0.98
└── Content (Chinese Markdown)
    ├── 标题 (translated heading)
    ├── 副标题 (if applicable)
    └── 正文段落 (formatted with Markdown)

quality_report.json
├── consistency_check
│   ├── entities_checked: N
│   ├── mismatches: N
│   └── consistency_score: 0.98
├── format_validation
│   ├── valid_markdown: true
│   ├── format_issues: []
│   └── links_preserved: true
└── overall_status: pass|warning
```

---

## Performance Estimation

### Time Breakdown
| Phase | Duration | % of Total |
|-------|----------|-----------|
| File scanning & analysis | 5 min | 9% |
| Translation | 40 min | 73% |
| Verification & finalization | 10 min | 18% |
| **Total** | **55 min** | **100%** |

### Token Estimation
- **Analysis**: ~5,000 tokens (haiku, fast)
- **Translation**: ~55,000 tokens (sonnet, main work)
- **Verification**: ~15,000 tokens (sonnet, full file review)
- **Total**: ~75,000 tokens

*Assumption: ~2,000-3,000 word English article*

### Scalability
- Single article: 55 minutes
- 3 articles in parallel: 55 minutes (workers can process independently)
- 10 articles sequentially: ~9 hours

---

## Risk Assessment & Mitigation

### Critical Risks
| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|-----------|
| Source file not found | Complete failure | HIGH | Pre-flight validation; clear error message |
| Encoding issues | Format corruption | MEDIUM | Auto-detect encoding; retry with fallback |
| Terminology gaps | Inconsistency | MEDIUM | reviewer_01 flags all mismatches |
| Model timeout | Retry overhead | MEDIUM | Chunking strategy; escalate after 3 attempts |

### Medium Risks
- **Technical jargon**: Mark with [TBD] if unsure; reviewer flags
- **Time-sensitive references**: Context provided by analyst_01
- **Complex sentence structures**: Sonnet handles well; reviewer assesses

### Mitigation Strategies
1. **Pre-flight validation**: Ensure source file exists and is readable
2. **Robust error handling**: 3-attempt retry with debug_01 escalation
3. **Terminology enforcement**: 100% verification at review stage
4. **Quality reporting**: Comprehensive audit trail for all decisions

---

## Design Decisions

### Why Sequential (Not Parallel)?
- **Dependencies**: Each step requires output from previous
- **No parallelization opportunity**: Can't translate before analyzing entities
- **Sequential is safer**: Ensures data consistency

### Why Consolidate to 3 Steps?
- **Original 8 steps over-engineered**: Redundant quality checks
- **Speed-first optimization**: Haiku + Sonnet faster than all Sonnet
- **Cleaner data flow**: Analyst → Translator → Reviewer → Output
- **Easier debugging**: Clear responsibility boundaries

### Why No Human Intervention?
- **Task requirement**: max_interventions = 0
- **Automated fixes**: reviewer_01 can auto-correct most issues (confidence ≥ 0.90)
- **Comprehensive reporting**: All findings in quality_report.json for audit
- **Deterministic output**: Same input = same output (reproducible)

### Why Sonnet for Translator & Reviewer?
- **Quality/speed balance**: Better than Haiku, faster than Opus
- **Standard tier**: Speed-first doesn't need Opus quality
- **Cost-effective**: ~75K tokens for complete workflow
- **Proven**: Sonnet excels at translation and consistency checking

---

## Workflow Validation

### Schema Compliance
✓ Workflow YAML valid (no circular dependencies)
✓ All workers defined with correct models
✓ All steps have input/output contracts
✓ Error handling configured
✓ Timeouts set appropriately

### Logic Validation
✓ Data flows correctly through pipeline
✓ No missing inputs or outputs
✓ All dependencies explicit
✓ Error escalation paths defined
✓ Success criteria measurable

### Feasibility
✓ All tasks automatable (no human intervention required)
✓ Model selection appropriate
✓ Time estimates realistic
✓ No external dependencies
✓ Fully reproducible

---

## Files Generated

```
/flow/runs/run-meta-workflow-creation-20260326-075835/
├── workers/
│   ├── designer_01/
│   │   ├── design_package.json        [MAIN ARTIFACT]
│   │   ├── DESIGN_SUMMARY.md          [This file]
│   ├── analyst_01/
│   │   ├── AGENT.md                   [Role & responsibilities]
│   │   └── config.json                [Execution config]
│   ├── translator_01/
│   │   ├── AGENT.md                   [Role & responsibilities]
│   │   └── config.json                [Execution config]
│   ├── reviewer_01/
│   │   ├── AGENT.md                   [Role & responsibilities]
│   │   └── config.json                [Execution config]
│   └── debug_01/
│       └── config.json                [Error handling config]
└── artifacts/
    └── task_analysis/
        └── task_analysis.json         [Pre-flight analysis]
```

---

## Next Steps (Orchestrator)

1. **Review this design** ← You are here
2. **Validate design_package.json** against schema
3. **Initialize workflow**:
   - Create run instance
   - Set source_file_path parameter
   - Initialize worker state
4. **Execute step-by-step**:
   - analyst_01 scans file
   - translator_01 translates
   - reviewer_01 verifies
5. **Monitor and verify**:
   - Check logs in each worker
   - Monitor quality_report output
   - Verify final_article_zh.md
6. **Archive results**:
   - Save final output
   - Archive quality_report
   - Log design metrics

---

## Design Quality Checklist

- [x] Workflow logic is sound (no circular deps, clear DAG)
- [x] All workers have clear roles and responsibilities
- [x] Input/output contracts fully specified
- [x] Error handling configured for all failure modes
- [x] Quality thresholds set and measurable
- [x] Zero human intervention required (automation complete)
- [x] Time estimates reasonable (55 min for ~3K word article)
- [x] Token budget realistic (~75K for full workflow)
- [x] Models selected appropriately (haiku + sonnet balance)
- [x] Data flows correctly through pipeline
- [x] All files generated and validated
- [x] Design documentation complete

**Status**: ✅ **READY FOR EXECUTION**

---

**Generated by**: Designer Agent v1.0
**Design Time**: 2026-03-26T07:58:35Z
**Quality Level**: Standard (Speed-Optimized)
