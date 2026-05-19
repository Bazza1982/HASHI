# Auto Experiment Evidence Schema

Use this schema for the loop evidence artifact. Entries must be concrete,
command-backed, and include actual values — not summaries or qualitative
descriptions.

## Header

```text
loop_id:
template: auto_experiment
experiment_design_memo:
started_at:
orchestrator:
worker:
reviewer:
translation_api:
translation_model:
embedding_model:
exit_condition:
```

## Preflight Record

```text
db_connection: confirmed | failed
han_scripture_count:
pali_scripture_count:
known_parallels_count:
unmatched_han_count:
translation_api_status: healthy | degraded | failed
translation_api_test_response_ms:
embedding_api_status: confirmed | failed
embedding_model_dimensions:
translation_prompt_version:
translation_prompt_hash:
output_root:
```

## Phase 1 — Pair Classification

### Sample Record

```text
positive_pairs_count:
stratification_by_collection: {SA:, MA:, DA:, EA:}
stratification_by_length: {short:, medium:, long:}
stratification_by_correspondence_type: {full_exact:, resembling_tentative:}
stratification_by_structure: {1to1:, 1toMany:, manyTo1:}
negative_pairs_count: 300
easy_negative_count: 100
hard_negative_count: 100
near_miss_negative_count: 100
sample_file_path:
negatives_file_path:
```

### Translation Record

```text
translations_completed:
failed_translations:
model_version:
prompt_version:
total_tokens_used:
translations_output_path:
stability_sutras_count: 10
stability_instances_per_sutra: 3
mean_embedding_stability_std:
stability_output_path:
```

### Embedding Record

```text
strategies_computed: [whole-text, segment-max, chunk-512, structural]
texts_embedded_count:
embeddings_output_path:
total_tokens_used:
failures:
```

### Phase 1 Metrics

```text
best_strategy:
auc_roc_whole_text:
auc_roc_segment_max:
auc_roc_chunk_512:
auc_roc_structural:
pr_auc_best_strategy:
bootstrap_ci_lower_best_strategy:
per_tier_auc_easy:
per_tier_auc_hard:
per_tier_auc_near_miss:
per_collection_auc: {SA:, MA:, DA:, EA:}
per_length_auc: {short:, medium:, long:}
translation_stability_mean_std:
metrics_report_path:
```

### Phase 1 Reviewer Record

```text
reviewer: akane
review_surface: Phase 1 full metrics report
auc_tier2_assessment:
ci_lower_bound_assessment:
anomalies:
methodology_concerns:
gate_verdict: pass | conditional_pass | fail
gate_rationale:
```

### Phase 1 Gate Decision

```text
gate_outcome: pass | fail
gate_basis:
proceed_to_phase2: yes | no
fallback_if_fail:
```

## Phase 2 — Retrieval Simulation

### Embedding Record

```text
strategy_used:
texts_embedded_count:
embeddings_output_path:
failures:
```

### Retrieval Metrics

```text
queries_run:
recall_at_1:
recall_at_5:
recall_at_10:
recall_at_20:
mrr:
median_rank:
per_collection: {SA_recall10:, MA_recall10:, DA_recall10:, AN_recall10:}
per_correspondence_type: {full_exact_recall10:, resembling_tentative_recall10:}
metrics_report_path:
```

### Phase 2 Reviewer Record

```text
reviewer: akane
review_surface: Phase 2 retrieval metrics
recall_at_10_assessment:
mrr_assessment:
systematic_failure_patterns:
collection_anomalies:
gate_verdict: pass | conditional_pass | fail
gate_rationale:
```

### Phase 2 Gate Decision

```text
gate_outcome: pass | fail
gate_basis:
proceed_to_discovery: yes | no
fallback_if_fail:
```

### Discovery Record

```text
han_sutras_queried:
candidates_per_query: 20
score_threshold_used:
candidates_above_threshold:
candidates_file_path:
ea_sutras_queried:
ea_top_candidate_count:
ea_score_vs_known_pairs_assessment:
ea_report_path:
```

### Discovery Reviewer Record

```text
reviewer: akane
candidate_quality_assessment:
false_positive_patterns:
ea_candidate_assessment:
fitness_for_expert_review: yes | no | with_reservations
presentation_recommendations:
```

## Final Close

```text
memo_updated: yes | no
memo_path:
inbox_drain_completed: yes | no
pending_replies_count:
new_blockers_from_drain: yes | no
exit_condition_met: yes | no
all_gates_recorded:
open_risks:
next_recommended_steps:
closed_at:
```
