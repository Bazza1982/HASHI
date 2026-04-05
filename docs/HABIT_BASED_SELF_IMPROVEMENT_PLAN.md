# HASHI Habit-Based Self-Improvement Plan

> Status: design proposal
> Scope: long-term architecture and rollout plan
> Audience: HASHI core maintainers, Lily, agent/runtime implementers

---

## 1. Purpose

This document defines a comprehensive plan for a new HASHI capability:

**habit-based self-improvement**.

The goal is not merely to let agents remember facts. The goal is to let agents:

- reflect on what worked and what failed,
- convert repeated lessons into reusable behavioral habits,
- apply those habits before action,
- measure whether those habits actually improve outcomes,
- and allow Lily to evaluate habit effectiveness across the whole society of agents.

This is intended to solve persistent operational problems such as:

- agents repeatedly forgetting how to use `Hchat`,
- agents repeatedly forgetting how HASHI-managed cron jobs work,
- coding agents repeating the same debugging or verification mistakes,
- long-running project agents losing learned workflow discipline over time,
- local improvements never being tested or promoted systematically.

This plan is intentionally broader than an MVP. It is designed so that Phase 1 can be small, but the foundational model does not need to be redesigned later.

---

## 2. Strategic Positioning

Hermes-style self-improvement is primarily about remembering and reusing artifacts such as memory and skills.

HASHI should aim for something stronger:

**habit-evaluated, society-level self-improvement**.

In HASHI:

- individual agents generate and patch habits from `dream`,
- habits influence later execution,
- habits can be turned on or off,
- Lily does not blindly consolidate habits,
- Lily evaluates the correlation between habits and outcomes over time,
- only statistically credible habits are recommended for wider adoption.

This makes the system closer to an experimental learning architecture than a simple memory layer.

---

## 3. Design Goals

### 3.1 Primary Goals

- Reduce repeated user re-instruction on stable operational knowledge.
- Reduce recurrence of known mistakes.
- Make learning visible, inspectable, reversible, and measurable.
- Support both positive habits and avoidance habits.
- Preserve agent individuality while enabling system-wide evaluation.
- Keep the architecture compatible with existing HASHI components such as `/dream`, `bridge_memory.sqlite`, transcript logs, workspaces, and Lily-mediated memory consolidation.

### 3.2 Secondary Goals

- Support A/B-style comparison through `habit on/off`.
- Support promotion from local habit to shared recommendation without automatic overreach.
- Support different agent classes without requiring a separate system per class.
- Keep context overhead low enough for daily practical use.

### 3.3 Non-Goals

- This is not online weight training.
- This is not fully autonomous system policy mutation.
- This is not a replacement for long-term memory.
- This is not a replacement for formal workflows such as Nagare.
- This is not a promise that every useful behavior should become a habit.

---

## 4. Core Concepts

### 4.1 Habit

A **habit** is an agent-local behavioral rule extracted from reflection and intended to influence future execution.

A habit may encode:

- what to do,
- what to check first,
- what to avoid,
- what kind of trigger should activate it,
- what evidence created it.

### 4.2 Pattern Evidence

**Pattern evidence** is observational data about whether a habit helped, harmed, or had no clear impact.

This is not the habit itself. It is the evaluation trail.

### 4.3 Recommendation

A **recommendation** is Lily’s system-level judgment that a habit appears beneficial, harmful, limited to a certain context, or suitable for broader adoption.

Recommendations are advisory by default.

### 4.4 Promotion

**Promotion** is the act of moving from:

- agent-local habit
- to shared recommendation
- to optional shared pattern / protocol / policy

Promotion must never be fully automatic in the early phases.

---

## 5. High-Level Architecture

The system has two layers.

### 5.1 Individual Agent Layer

Responsible for:

- reflecting on success and failure,
- generating candidate habits from `dream`,
- retrieving relevant habits before task execution,
- logging whether a habit was triggered and whether it helped,
- patching, disabling, or refining habits after execution.

### 5.2 Lily Evaluation Layer

Responsible for:

- collecting habit usage evidence across agents,
- correlating habits with outcomes,
- comparing habit-enabled vs habit-disabled runs where possible,
- comparing dreamed vs non-dreamed behavior,
- generating recommendations,
- preventing low-quality local habits from becoming system truth.

### 5.3 Principle of Separation

This separation is essential:

- agents own local behavioral learning,
- Lily owns cross-agent evaluation,
- system-wide governance remains explicit and reviewable.

---

## 6. Why Habits Instead of Only Skills or Memory

Memory stores facts.

Skills store reusable procedures.

Habits store **execution tendencies**.

That distinction matters because many recurring failures in HASHI are not caused by lack of factual knowledge. They are caused by missing pre-action discipline, for example:

- forgetting to use the correct coordination channel,
- forgetting to verify before claiming success,
- forgetting to check environment assumptions before writing a cron job,
- forgetting to ask Lily when a memory question is actually cross-session.

These are better modeled as habits than as plain memory entries.

Habits are especially useful because they can encode both:

- `do` behavior,
- `avoid` behavior.

That second category is critical. A mature system must learn not only what to repeat, but what to stop repeating.

---

## 7. Proposed Object Model

### 7.1 Habit Schema

Each habit should have a structured schema from the start.

Suggested model:

```json
{
  "habit_id": "HASHI1_zelda_01JQ1Z7YJ6YJ4V6P6Q2Y8A3R9M",
  "agent_id": "zelda",
  "title": "Check Hchat before cross-agent assumptions",
  "description": "Before acting on cross-agent coordination or memory assumptions, verify whether Hchat or Lily is the correct path.",
  "status": "active",
  "enabled": true,
  "type": "do",
  "trigger": {
    "task_types": ["coordination", "memory", "handoff"],
    "keywords": ["Hchat", "lily", "memory", "cross-agent", "handoff"],
    "signals": ["another_agent_involved", "shared_context_needed"]
  },
  "guidance": "Check whether Hchat or Lily should be used before proceeding.",
  "avoidance": "Do not assume other agents already have the relevant context.",
  "rationale": "Past failures were caused by implicit coordination assumptions.",
  "scope": "agent-local",
  "priority": 70,
  "confidence": 0.62,
  "source_episodes": ["dream:2026-04-02", "session:abc123"],
  "created_at": "2026-04-02T21:30:00+11:00",
  "updated_at": "2026-04-02T21:30:00+11:00",
  "stats": {
    "times_triggered": 0,
    "times_applied": 0,
    "times_helpful": 0,
    "times_harmful": 0,
    "times_ignored": 0,
    "last_triggered_at": null,
    "last_helpful_at": null
  }
}
```

### 7.2 Required Fields

At minimum, the schema should include:

- `habit_id`
- `agent_id`
- `type`
- `enabled`
- `trigger`
- `guidance`
- `scope`
- `confidence`
- `source_episodes`
- `stats`

`habit_id` should not be a local counter. Phase 0 should standardize on an instance-scoped ULID-style format such as `{instance}_{agent_id}_{ulid}` to avoid collisions across workspaces and future copying flows.

### 7.3 Habit Types

The system should support at least:

- `do`: recommended action pattern
- `avoid`: pitfall avoidance
- `check`: mandatory verification tendency
- `escalate`: when to ask for help, notify Lily, or open a ticket

`do` and `avoid` are enough for MVP, but the data model should leave room for the others.

### 7.4 Scope Types

Possible scopes:

- `agent-local`
- `agent-class`
- `project-local`
- `system-recommended`
- `system-policy`

Phase 1 should use only `agent-local`, but the schema should support later scope promotion.

---

## 8. Storage Model

### 8.1 Recommended Storage Layers

The system should distinguish the following:

- `habit store`: current active and inactive habits
- `habit event log`: per-run evidence about triggering, application, and effect
- `evaluation output`: Lily’s reports and recommendations

### 8.2 Suggested File / Data Locations

These are proposed paths, not a hard implementation mandate:

- `workspaces/<agent>/habits.json` or `workspaces/<agent>/habits.sqlite` for local habit state
- a dedicated `habit_events` table in a separate evaluation database such as `habit_evaluation.sqlite`, or a clearly separate table in Lily’s evaluation database
- `workspaces/<agent>/habit_state.json` for local toggles if needed
- evaluation output in Lily-owned storage
- `docs/` for design and policy references

### 8.3 Why Separate State from Events

The current habit definition should be easy to read and patch.

The event stream should be append-only and optimized for evaluation.

Mixing both into one file will create friction later.

More importantly, habit events must not be mixed into the same semantic memory store used for natural-language memory retrieval. Boolean evaluation metadata such as `triggered`, `applied`, `helpful`, and `harmful` will pollute vector search if stored beside conversational memory. Habit evaluation data should live in a structurally separate table or database.

### 8.4 Recommended Habit Event Schema

Phase 0 should freeze a versioned event schema early. A minimal shape is:

```sql
CREATE TABLE habit_events (
    id INTEGER PRIMARY KEY,
    version TEXT NOT NULL,
    instance TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    habit_id TEXT NOT NULL,
    task_type TEXT,
    triggered BOOLEAN NOT NULL,
    applied BOOLEAN NOT NULL,
    helpful BOOLEAN DEFAULT NULL,
    harmful BOOLEAN DEFAULT NULL,
    ignored BOOLEAN DEFAULT FALSE,
    context_summary TEXT,
    ts TEXT NOT NULL,
    ts_source TEXT NOT NULL,
    UNIQUE(instance, agent_id, habit_id, ts)
);
```

Recommended `ts_source` values:

- `native`
- `turns_match`
- `mtime_fallback`

Evaluation should be able to filter by `ts_source` and exclude low-confidence timestamps.

---

## 9. Integration with Existing HASHI Components

### 9.1 `/dream`

`/dream` already performs nightly reflection and memory consolidation. It is the natural source of candidate habits.

Future direction:

- keep the existing memory behavior,
- extend dream output schema to include habit candidates,
- optionally include habit patch proposals,
- optionally include disable proposals for stale or harmful habits.

### 9.2 Transcript and Recent Context Logs

Existing logs already provide rich evidence:

- `transcript.jsonl`
- `recent_context.jsonl`
- `handoff.md`
- execution and token logs

These should be used as evidence sources, not duplicated into a new logging system unless necessary.

However, timestamp quality is uneven in historical transcript data. For evaluation that depends on before/after comparisons, Lily should prefer records with native timestamps. Phase 1 evaluation should default to post-`2026-04-02` data that carries native `ts` values, while older backfilled timestamps remain available only for coarse analysis.

### 9.3 Lily

Lily should remain the sole system-wide memory guardian.

Habit evaluation should not weaken that principle.

Lily’s role should be:

- evaluator,
- recommender,
- evidence consolidator,
- promotion gatekeeper.

### 9.4 AGENT.md

Habit logic should not be implemented only by mutating `AGENT.md`.

`AGENT.md` is durable agent identity and role guidance. Habits are dynamic execution modifiers.

Some mature habits may later justify a role update, but habit state should remain its own system.

### 9.5 Skills

Skills and habits should be complementary:

- skills answer "how to do this procedure",
- habits answer "what should I remember to do or avoid before/during execution".

---

## 10. Habit Lifecycle

### 10.1 Stage 1: Observation

The system observes success, failure, confusion, user correction, retries, and repeated friction.

### 10.2 Stage 2: Dream Reflection

At dream time, the agent asks:

- what worked repeatedly,
- what failed repeatedly,
- what should become a reusable behavioral rule,
- what should explicitly be avoided next time.

### 10.3 Stage 3: Candidate Generation

Dream produces:

- `new_habit_candidates`
- `patch_habit_candidates`
- `disable_habit_candidates`

### 10.4 Stage 4: Activation

Approved candidates become active habits in the local habit store.

### 10.5 Stage 5: Retrieval Before Action

At task start, the agent performs a lightweight habit retrieval step and receives only the most relevant habits.

### 10.6 Stage 6: Execution Logging

During or after execution, the system records:

- which habits triggered,
- which were shown,
- which were applied,
- whether they helped,
- whether they were ignored,
- whether they appear too broad or too narrow.

### 10.7 Stage 7: Evaluation

Lily periodically aggregates events and produces recommendations.

### 10.8 Stage 8: Promotion, Restriction, or Retirement

Over time, a habit may be:

- promoted,
- copied to similar agents,
- restricted to a narrower context,
- disabled,
- archived.

---

## 11. Habit Retrieval Model

### 11.1 Retrieval Objective

The system should retrieve a very small number of highly relevant habits before execution.

### 11.2 Input Signals

Matching can use:

- user message keywords,
- synonym dictionaries and multilingual variants,
- command type,
- tool type,
- workflow type,
- whether multiple agents are involved,
- whether long-term memory is implicated,
- whether the action is high risk,
- whether the task is coding, operational, analytical, or social.

### 11.3 Output Constraints

Recommended limits:

- maximum 3 habits returned,
- at most 2 positive habits,
- at most 1 avoidance habit,
- all rendered as concise execution guidance.

This keeps context overhead acceptable.

The top-3 cap should be a hard runtime limit, not a soft prompt suggestion.

### 11.4 Example

If the task mentions `cron`, relevant habits may include:

- verify runtime user and environment,
- log output path before scheduling,
- do not claim success until schedule behavior is validated.

---

## 12. Habit On / Off Controls

This system must support explicit toggling.

### 12.1 Why Toggle Support Is Necessary

- enables safe experimentation,
- allows debugging of harmful habits,
- prevents overfitting in unusual tasks,
- allows cleaner evaluation of whether habits matter.

### 12.2 Required Levels

- global per-agent toggle
- single-habit enable / disable
- session-level temporary bypass

### 12.3 Suggested Commands

Potential future commands:

- `/habit on`
- `/habit off`
- `/habit status`
- `/habit list`
- `/habit enable <id>`
- `/habit disable <id>`
- `/habit explain <id>`

Command design can come later. The architecture should assume these controls will exist.

---

## 13. Common Use Cases by Agent Type

The plan must work for more than one kind of agent. This section defines representative use cases.

### 13.1 Coding Agents

Typical repeated failures:

- forgetting to inspect existing code patterns before editing,
- forgetting to run verification,
- forgetting to consider dirty worktrees,
- claiming a fix without testing,
- using the wrong coordination channel for system knowledge.

Representative habits:

- always inspect nearby code before patching,
- verify with targeted tests when possible,
- do not revert unrelated changes,
- if system memory or cross-agent coordination is involved, check Lily / Hchat path,
- when task risk is high, prefer evidence from repository state before inference.

### 13.2 Long-Running Project Agents

Typical repeated failures:

- losing project continuity,
- re-asking already resolved project decisions,
- drifting from established conventions,
- forgetting deliverable state or pending dependencies.

Representative habits:

- check project memory before proposing a new direction,
- do not reopen settled decisions without new evidence,
- summarize state transitions explicitly,
- escalate unresolved dependency blockers instead of silently continuing.

### 13.3 Coordinator / Manager Agents

Typical repeated failures:

- assuming shared context,
- forgetting handoff discipline,
- sending ambiguous requests,
- failing to capture decisions in the right place.

Representative habits:

- when another agent is involved, explicitly state context and target outcome,
- ask Lily to store important project-level decisions,
- do not assume prior knowledge across sessions,
- verify ownership before delegating.

### 13.4 Support / Operations Agents

Typical repeated failures:

- skipping environment verification,
- giving commands without host/user/path clarity,
- forgetting logs, permissions, or restart impact,
- failing to distinguish recommendation vs safe automatic fix.

Representative habits:

- verify runtime environment before operational change,
- never claim cron success without evidence,
- log paths and execution user are mandatory,
- if confidence is low, recommend rather than mutate.

### 13.5 Research / Analyst Agents

Typical repeated failures:

- presenting uncertain inference as fact,
- failing to track source quality,
- not distinguishing current data from stable knowledge,
- not stating assumptions.

Representative habits:

- separate observed fact from inference,
- attach evidence quality to conclusions,
- when current data matters, verify freshness,
- do not generalize from one example without stating limitations.

### 13.6 Persona / Relationship-Heavy Agents

Typical repeated failures:

- forgetting user-specific preferences,
- breaking character continuity,
- leaking system-style responses into persona-heavy interactions,
- missing when information should be stored via Lily.

Representative habits:

- preserve role voice while keeping factual claims grounded,
- route durable user facts to Lily rather than relying on session context alone,
- do not overwrite established relationship facts without evidence,
- separate emotional tone from operational certainty.

---

## 14. Common Use Cases by Failure Mode

The system should be designed around recurring failures, not just agent classes.

### 14.1 Repeated Instruction Failure

Example:

- user teaches `Hchat` usage repeatedly.

Desired outcome:

- a habit is formed,
- it triggers before relevant coordination tasks,
- user re-instruction frequency drops measurably.

### 14.2 Repeated Operational Failure

Example:

- agent repeatedly forgets correct cron-job setup discipline.

Desired outcome:

- the agent reliably checks environment, user, logging, verification, and scope before claiming success.

### 14.3 Repeated Verification Failure

Example:

- coding agent edits files but skips validation.

Desired outcome:

- verification becomes a habitual pre-completion behavior.

### 14.4 Repeated Coordination Failure

Example:

- agents assume context was shared when it was not.

Desired outcome:

- coordination habits force explicit communication boundaries.

### 14.5 Repeated Overconfidence Failure

Example:

- analysis or support agents present uncertain claims too strongly.

Desired outcome:

- habits enforce uncertainty labeling and escalation.

---

## 15. Evaluation Framework

Lily’s evaluation job is the heart of the system-wide advantage.

### 15.1 Minimum Metrics

The system should record enough to evaluate at least:

- task success rate,
- user re-instruction rate,
- recurrence of same mistake,
- retry count,
- recovery time,
- verification completion rate,
- escalation rate,
- habit-trigger rate,
- habit-application rate,
- habit-helpfulness rate,
- habit-harmfulness rate.

### 15.2 Important Comparison Axes

Lily should compare outcomes across:

- habit enabled vs habit disabled,
- dreamed vs non-dreamed periods,
- agent class,
- task type,
- backend type,
- repeated task families,
- before-habit vs after-habit periods.

### 15.3 Qualitative Signals

Not all evidence will be numeric.

Qualitative signals should also be captured, such as:

- user explicitly correcting the same issue again,
- user explicitly praising improved consistency,
- the agent noting that a habit prevented a likely mistake,
- human review determining that a habit caused over-caution or confusion.

### 15.4 Statistical Maturity

Early phases do not need perfect significance testing, but the data model should support it later.

Initially, Lily can use:

- repeated positive evidence threshold,
- repeated negative evidence threshold,
- per-task-family comparison,
- confidence scoring with decay and accumulation.

Phase 1 should also apply a minimum sample threshold before any promotion-style recommendation. A simple initial rule is `min_sample_for_promotion = 10`.

Later phases can add:

- significance tests,
- propensity adjustment,
- task family baselines,
- stronger causal heuristics.

---

## 16. Recommendation and Promotion Rules

Recommendations should be explicit and conservative.

### 16.1 Recommendation Types

Lily may recommend:

- keep observing,
- narrow scope,
- broaden scope,
- copy to similar agents,
- convert to shared pattern,
- convert to formal protocol,
- retire habit.

### 16.2 Promotion Preconditions

A habit should generally not be promoted unless:

- it has repeated successful evidence,
- it has reached the minimum sample threshold,
- its trigger conditions are clear,
- it does not appear to harm other task families,
- the target agent class is comparable,
- a human approves promotion in early phases.

### 16.3 Why Promotion Must Be Conservative

Local optimization can be harmful if generalized.

A habit that helps a support agent may slow down a creative agent.

A habit that helps coding reliability may degrade responsiveness in casual conversation.

---

## 17. Guardrails and Risk Control

### 17.1 Habit Explosion

Risk:

- too many habits accumulate and retrieval quality collapses.

Controls:

- strict candidate criteria,
- top-k retrieval,
- confidence decay,
- archival rules,
- habit merging.

### 17.2 Bad Habit Consolidation

Risk:

- a locally useful but broadly harmful pattern becomes sticky.

Controls:

- evidence tracking,
- disable path,
- negative evidence weighting,
- conservative promotion,
- human approval.

### 17.3 Context Pollution

Risk:

- too many habits consume execution context.

Controls:

- concise rendering,
- retrieval cap,
- relevance scoring,
- no full history injection.

### 17.4 Identity Drift

Risk:

- dynamic habits accidentally rewrite stable persona / role identity.

Controls:

- keep habits outside `AGENT.md`,
- limit `AGENT.md` updates to durable role-level insights,
- separate dynamic behavior from core character.

### 17.5 Evaluation Bias

Risk:

- habits look useful because only easy tasks triggered them.

Controls:

- compare by task family,
- compare by agent class,
- log when habit was triggered but ignored,
- log when habit was enabled but not applicable.

### 17.6 Timestamp Credibility

Risk:

- backfilled timestamps create false before/after correlations.

Controls:

- include `ts_source` on all evaluable records,
- filter promotion and habit-effect evaluation to high-confidence timestamps,
- treat `mtime_fallback` records as low-confidence evidence only,
- default Phase 1 analysis to native timestamp data.

### 17.7 Storage Pollution

Risk:

- habit evaluation metadata pollutes semantic memory retrieval.

Controls:

- store habit events in a dedicated table or database,
- do not embed habit event rows into the conversational vector index,
- join evaluation results at report time rather than retrieval time.

---

## 18. Candidate Selection Rules for Dream

Dream should not turn every lesson into a habit.

### 18.1 A Candidate Habit Should Usually Satisfy These Conditions

- likely to recur,
- behaviorally actionable,
- not merely a one-off fact,
- not just a user preference unless stable,
- triggerable by future context,
- meaningfully changes execution.

### 18.2 Things That Should Usually Not Become Habits

- one-time situational details,
- pure factual memory without behavioral consequence,
- temporary environment states,
- vague advice that cannot influence action,
- abstract moral slogans with no trigger or behavior.

---

## 19. Proposed Dream Output Extension

The current dream JSON output can evolve toward:

```json
{
  "new_memories": [],
  "agent_md_updates": [],
  "new_habit_candidates": [],
  "patch_habit_candidates": [],
  "disable_habit_candidates": [],
  "reflection_summary": ""
}
```

Habit candidate objects should be validated before activation.

Early phases may require:

- schema validation,
- basic duplicate detection,
- optional human approval,
- or a simple policy such as "auto-activate only low-risk local habits".

---

## 20. Suggested Runtime Hooks

These are architecture hooks, not mandatory implementation details.

### 20.1 Pre-Execution Hook

Before normal execution:

- derive task features,
- retrieve relevant active habits,
- inject compact guidance.

### 20.2 Post-Execution Hook

After task completion:

- record whether habits were triggered,
- record whether the task succeeded,
- record whether the user had to correct the same issue again,
- create patch hints if the habit looked incomplete.

### 20.3 Nightly Evaluation Hook

At Lily consolidation time:

- aggregate habit events,
- update confidence and evidence,
- produce recommendations.

---

## 21. Rollout Plan

The system should be built in phases.

### Phase 0: Design Freeze

Deliverables:

- finalize terminology,
- finalize habit schema,
- finalize event schema,
- define which outputs belong to dream, runtime, and Lily,
- define timestamp-confidence policy,
- define agent classification.

Exit criteria:

- no unresolved ambiguity about habit vs memory vs skill vs policy.

Phase 0 should explicitly freeze:

- `habit_events` stored separately from conversational memory tables,
- ULID-style `habit_id` format with instance prefix,
- `last_triggered_at` and `last_helpful_at` in habit stats,
- `agent_class` added to agent configuration,
- `min_sample_for_promotion = 10`,
- retrieval hard-capped at top 3,
- `ts_source` recorded on evaluable events,
- versioned event schema from day one.

### Phase 1: Habit MVP

Scope:

- agent-local habits only,
- no automatic sharing,
- no statistical promotion,
- focus on two repeated pain points.

Recommended first habit families:

- `Hchat / Lily coordination discipline`
- `cron / scheduled task discipline`

Deliverables:

- local habit store,
- dream candidate generation,
- pre-execution retrieval,
- post-execution event log,
- `habit on/off`,
- basic status inspection.

Exit criteria:

- users see fewer repeated instruction failures in these domains.

### Phase 2: Evaluation Foundation

Scope:

- Lily reads habit events,
- basic confidence updates,
- recommendation reports,
- per-agent and per-task-family summaries.

Constraints:

- promotion-oriented evaluation uses only high-confidence timestamp records by default,
- historical backfilled timestamps may inform exploration but not causal-sounding comparisons,
- confidence must decay over time when habits stop triggering or stop proving helpful.

Deliverables:

- evidence aggregation pipeline,
- recommendation format,
- weekly or nightly report output.

Exit criteria:

- Lily can identify which local habits appear helpful or harmful.

### Phase 3: Class-Level Recommendations

Scope:

- support recommendation copying to similar agents,
- no full automation yet,
- approval-based promotion.

Deliverables:

- agent class mapping,
- recommendation-to-copy workflow,
- audit trail for copied habits.

Exit criteria:

- at least one habit family can be safely reused across similar agents.

### Phase 4: Shared Patterns and Protocols

Scope:

- formalize mature habits into shared patterns or system protocols.

Deliverables:

- shared pattern registry,
- protocol documentation,
- explicit ownership and governance rules.

Exit criteria:

- promoted rules are reviewable, documented, and measurably beneficial.

### Phase 5: Advanced Evaluation

Scope:

- stronger statistical analysis,
- richer dashboards,
- intervention quality comparison across backends and agent classes.

Possible additions:

- confidence decay models,
- significance testing,
- exception analysis,
- automatic "observe more" recommendations.

---

## 22. Implementation Planning Considerations

Even if implementation starts small, the following should be decided early to avoid rework.

### 22.1 Identity Strategy

Habit IDs must remain stable across patches.

They should also remain globally unique across instances, which makes ULID-style IDs preferable to local counters.

### 22.2 Event Schema Stability

Event logs should be versioned from the start.

They should also include `ts_source` from the first version so evaluation code does not need a later migration just to separate native timestamps from inferred ones.

### 22.3 Backward Compatibility

Agents without habits should still run normally.

### 22.4 Human Review Surfaces

There should be a clear path to inspect:

- what habits exist,
- where they came from,
- why Lily recommended promotion or retirement.

### 22.5 Failure-Tolerant Defaults

If habit retrieval fails, execution should continue rather than block.

If evaluation fails, local habits should still function.

### 22.6 Confidence Decay

Confidence should not be a lifetime cumulative score.

At minimum, the system should track `last_triggered_at` and `last_helpful_at`, and decay stale habits after a defined inactivity window. A reasonable initial default is:

- decay after 14 days without triggering,
- mark as archive candidate after 28 days without triggering or fresh helpful evidence.

---

## 23. Open Design Questions

These questions should be resolved during Phase 0 or early Phase 1.

- Should habit candidates auto-activate by default, or require approval?
- Should habits be stored in JSON files, SQLite, or both?
- Should some low-risk habit families be generated without dream, directly from runtime review?
- Should Lily own promotion approval, or should Lily only recommend and defer to the human?
- Should habit retrieval occur for every message, or only for high-signal task starts?
- Which event fields are mandatory for useful evaluation without creating too much overhead?

Agent classes should be defined in config before Phase 3 begins, not inferred ad hoc from prompts or names.

---

## 24. Recommended First Success Criteria

The first version should be judged by concrete improvement in a few repeated pain points.

Recommended success criteria:

- users no longer need to repeatedly reteach `Hchat` usage to the same agents,
- cron-related operational guidance becomes more consistent,
- repeated mistake frequency drops for tracked failure families,
- agents can explain which habits influenced a task when asked,
- Lily can produce at least one meaningful recommendation report from real evidence.

---

## 25. Summary

HASHI should not stop at memory consolidation.

The stronger architecture is:

- `dream` creates candidate habits from success and failure,
- habits influence future execution,
- habits include both positive discipline and pitfall avoidance,
- Lily evaluates outcomes across agents and time,
- only credible patterns are promoted.

This creates a system that does not merely remember.

It learns behavior, tests that behavior, and governs that behavior across a society of agents.

That is the foundation for a genuinely stronger self-improving agentic system.
