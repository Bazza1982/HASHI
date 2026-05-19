# Auto Experiment Superloop Template

## Purpose

Run a bounded empirical experiment loop where one orchestrator coordinates one
worker agent and one independent reviewer until the experiment is completed in
full — from preflight through final results and evidence record.

This template is for multi-phase data experiments requiring: preflight
validation, structured execution with quality gates, translation or
computational steps, independent statistical review, and a reproducible
evidence trail. It is not a coding template. It closes only when all phases
have passed their quality gates and exit evidence has been recorded.

## Standard Roles

- `orchestrator`: Arale or the active controller. Owns task framing, scope,
  quality gate decisions, phase advancement, and final close. Does not execute
  data tasks directly.
- `worker`: Feiyan or another computational agent. Executes all data
  retrieval, translation, embedding, and metric computation tasks. Reports
  results with concrete evidence (row counts, scores, file paths).
- `reviewer`: Akane or another independent verifier. Reviews methodology,
  data quality, statistical validity, and result interpretation at each quality
  gate. Makes pass/conditional-pass/fail decisions with written rationale.
- `consultant`: Optional. Used for repeated quality gate failures, novel
  statistical questions, or when methodology needs redesign.

The worker executes; the reviewer challenges; the orchestrator decides and
advances.

## Inputs

- Experiment design document (memo path or inline spec).
- Database connection string and expected data counts.
- Translation API endpoint and model.
- Embedding API key and model name.
- Phase quality gate thresholds.
- Exit condition (all phases complete, all gates passed, evidence recorded).
- Output directory for artifacts.

## Non-Negotiable Gates

Before starting, read the shared balanced orchestration guidance:

```text
superloops/config/orchestration_guidance.json
```

### G0 Preflight

Before any experiment work begins:

- Verify database connectivity and expected data counts
- Verify translation API health and model availability
- Verify embedding API access
- Lock translation prompt version and record it in evidence
- Create output directory structure
- Record all environment details

Do not proceed to Phase 1 if preflight fails. Record failure and pause for
operator resolution.

### G1 Phase 1 Quality Gate

After Phase 1 metrics are computed, reviewer evaluates:

- AUC-ROC against Tier 2 (hard) negatives
- 95% bootstrap confidence interval lower bound
- Translation stability score
- Per-stratum breakdown for anomalies

**Pass threshold**: AUC > 0.80 against Tier 2, CI lower bound > 0.70.

If gate fails: orchestrator decides whether to retry with adjusted parameters
(different embedding strategy, translation prompt change) or abort with
documented reason. Do not proceed to Phase 2 on a failed Phase 1.

### G2 Phase 2 Quality Gate

After Phase 2 retrieval metrics are computed, reviewer evaluates:

- Recall@10 across all 1,040 known pairs
- MRR
- Per-collection breakdown for systematic failures

**Pass threshold**: Recall@10 > 60%, MRR > 0.40.

If gate fails: document result as falsification of the embedding approach;
record fallback recommendation (translate both to English); do not run
discovery.

### G3 Discovery Review Gate

After discovery run on unmatched sutras, reviewer evaluates:

- Score distribution of top candidates relative to known true pairs
- EA/AN candidate quality
- Any systematic false-positive patterns
- Fitness for expert human review

Discovery candidates are candidates, not findings. Reviewer confirms fitness
for forwarding to expert review, not scholarly truth.

### G4 Evidence-First Reporting

Every worker report must include:

- exact data counts queried
- parameters used (model version, prompt version, temperature)
- output file paths
- specific metric values with units
- failures or anomalies observed

Do not accept "done" or "looks good" without concrete values.

### G5 Inbox Drain Before Close

Before final close:

- check queued worker/reviewer replies for the loop id
- classify every pending reply as current evidence, superseded, contradiction,
  or new blocker
- reopen if any late reply contains a new blocker or contradictory evidence

### G6 Exit Condition

The loop exits when and only when:

- all phases completed or formally skipped with documented reason
- all quality gates passed or formally recorded as failed with fallback noted
- discovery run complete (or skipped if Phase 2 gate failed)
- research memo updated with actual results
- all pending replies drained and classified
- exit evidence record complete

## Orchestrator Supervision Policy

Long-running worker tasks (translation batches, embedding runs) take minutes to
hours. The orchestrator must not wait passively. These supervision mechanisms
are required for every worker task that takes more than two minutes.

### Required: Output File Monitor

For every worker task that writes to a file, arm a Monitor before dispatching:

```bash
# Example for translation output
while true; do
  if [ -f "$OUTPUT_FILE" ]; then
    lines=$(wc -l < "$OUTPUT_FILE")
    errors=$(grep -c '"status": "failed"' "$OUTPUT_FILE" || echo 0)
    echo "[loop_id] task_id progress: ${lines}/${total} done, ${errors} errors"
    [ "$lines" -ge "$total" ] && echo "[loop_id] task_id COMPLETE" && break
  else
    echo "[loop_id] task_id waiting: output file not yet created"
  fi
  sleep 60
done
```

The Monitor command must cover both success and failure paths — silence is
not success. Record the Monitor task ID in `waits.json`.

### Required: ScheduleWakeup Fallback Heartbeat

After dispatching any long-running worker task, schedule a wakeup within
the cache window (≤270s) for quick tasks, or at 1200s for tasks expected
to take many minutes:

- On wakeup: check Monitor output and file state
- If task complete: advance to next step
- If task partial: log progress, reschedule wakeup
- If task stale (no progress in 2× expected time): send hchat status ping
  to worker, record in issues.json as `stale_worker` severity=medium

### Required: Stale Worker Protocol

If a worker has not produced output or replied within 2× the expected task
duration:

1. Check if any output file exists and inspect partial output
2. Send hchat status ping: "loop_id task_id: still working? please send
   progress update or report blocker"
3. Record in issues.json with severity=medium, status=monitoring
4. If no reply within another 30 minutes: escalate to operator

### Required: Worker Heartbeat Request

For tasks expected to take >20 minutes, include in the dispatch message:
"Please send a brief progress update every 10–15 minutes even if not done."

### Anti-Patterns

- Dispatching a long task and waiting passively for hchat reply only
- Starting a Monitor that only watches for the happy path (success marker)
- Not recording the Monitor task ID — you cannot stop it if it runs stale
- Forgetting ScheduleWakeup when a Monitor is the primary wake signal

---

## Translation Protocol

All Han-to-modern-Chinese translations use the HASHI API gateway:

- Endpoint: configured per-instance in loop state
- Model: claude-sonnet-4-6 (or as specified in instance)
- Temperature: 0
- Prompt: version-locked (stored in evidence)
- No summarisation, no glosses, no doctrinal additions
- Terminology locked: 比丘, 阿难, 世尊, 涅槃, 无常, 苦, 集, 灭, 道, 五蕴,
  六处, 十二因缘, 八正道, 三宝, 四圣谛, 禅定

Translation outputs saved to output directory with scripture ID, model version,
prompt version, and timestamp.

## Embedding Protocol

All embeddings use text-embedding-3-large (OpenAI, 3,072 dimensions).
Four strategies computed in parallel:

- whole-text: full concatenated text as single string
- segment-max: embed each segment; take max similarity across segment pairs
- chunk-512: overlapping 512-token chunks; aggregate top-3 chunk similarities
- structural: title + opening formula + doctrinal topic keywords only

Best strategy from Phase 1 is used for Phase 2 retrieval.

## Standard Loop

1. Preflight: verify DB, APIs, lock prompt version, create output structure.
2. Phase 1 sample selection: draw 100 positive pairs + 300 negatives (3 tiers).
3. Phase 1 translation: translate 100 Han sutras + stability test.
4. Phase 1 embedding: embed all texts, 4 strategies.
5. Phase 1 metrics: compute AUC, PR-AUC, bootstrap CI, per-tier, per-stratum.
6. Phase 1 reviewer quality gate (G1).
7. Orchestrator Phase 1 gate decision. If fail: document and stop.
8. Phase 2 embedding: embed all 3,413 Pali texts with best Phase 1 strategy.
9. Phase 2 retrieval: run all 1,040 known pairs as queries.
10. Phase 2 metrics: Recall@k, MRR, per-collection breakdown.
11. Phase 2 reviewer quality gate (G2).
12. Orchestrator Phase 2 gate decision. If fail: record fallback, skip discovery.
13. Discovery run: embed 625 unmatched Han sutras, collect top-20 candidates.
14. EA/AN special candidate report.
15. Discovery reviewer gate (G3).
16. Update research memo with actual results.
17. Drain and classify all pending worker/reviewer replies.
18. Record exit evidence and close.

## Anti-Patterns

- Advancing to Phase 2 before Phase 1 gate is reviewed and approved.
- Accepting metric summaries without seeing actual score distributions.
- Treating discovery candidates as confirmed findings.
- Closing the loop because phases were executed without checking gate results.
- Skipping the EA/AN special report — it is a primary research output.
- Updating the research memo before final reviewer gate is passed.
- Closing while worker/reviewer replies for the loop remain unclassified.
