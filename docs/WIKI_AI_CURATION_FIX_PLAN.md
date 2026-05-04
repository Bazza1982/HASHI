# Wiki AI Curation Fix Plan

Status: draft for review  
Owner: HASHI wiki pipeline  
Created: 2026-05-04  

## 1. Problem Statement

The current wiki pipeline is not yet an AI-maintained knowledge curation system. It is an LLM-assisted fixed-label classification system.

This distinction matters because the wiki is meant to serve agents as a fast knowledge access layer. Agents need the wiki to surface durable projects, concepts, decisions, current state, and open questions. The current system can only publish pages for topics that already exist in a hardcoded taxonomy.

Observed failure examples:

- `KASUMI` exists as clear software/project knowledge but is folded into `Minato_Platform`.
- `Manchuria` exists as a clear game project but is only discoverable through Daily logs.
- `WORDO`, `AIPM`, `Nexcel`, `Zotero`, `Deep Dive Audio Paper`, and Sakura Ref-style work appear in `UNCATEGORIZED_REVIEW` rather than becoming first-class wiki topics.

The underlying failure is architectural: there is no topic discovery, topic promotion, AI page synthesis, or knowledge curation loop.

## 2. Current Failure Points

### 2.1 Hardcoded Topic Taxonomy

File: `scripts/wiki/config.py`

`TOPICS` is a Python dictionary. The classifier and page generator both depend on it directly.

Failure:

- New projects cannot become visible unless a developer edits Python code.
- AI cannot create, suggest, merge, split, or retire topics.
- The taxonomy is a deployment artifact, not a knowledge artifact.

### 2.2 Closed Classifier Prompt

File: `scripts/wiki/classifier.py`

The prompt requires:

```text
Output only valid topic IDs from the list above.
```

The parser then rejects unknown topic IDs.

Failure:

- Even if the LLM recognizes `Manchuria_Game` as a coherent topic, it is forbidden to output it.
- `UNCATEGORIZED_REVIEW` becomes a sink rather than a discovery queue.
- The LLM is used as a tagger, not as a curator.

### 2.3 No Topic Discovery Stage

File: `scripts/wiki/run_pipeline.py`

Current flow:

```text
fetch -> classify -> persist -> generate pages -> publish vault
```

Failure:

- No step scans `UNCATEGORIZED_REVIEW`.
- No step clusters repeated entities or projects.
- No step compares candidate topics against existing topics.
- No step writes candidate topics to a registry.

### 2.4 Evidence Dump Pages Instead of Synthesized Knowledge

File: `scripts/wiki/page_generator.py`

Generated pages currently contain scope plus recent evidence snippets.

Failure:

- Pages do not answer agent questions efficiently.
- Agents must read raw evidence rather than stable facts.
- The page generator does not distinguish current state, decisions, risks, open questions, or source evidence.

### 2.5 Fixed Evidence Window

File: `scripts/wiki/page_generator.py`

Each topic uses:

```text
max_memories_per_topic = 30
```

Failure:

- Historical project definitions can be dropped.
- Early architecture decisions disappear from generated pages.
- Recent noisy updates can crowd out foundational knowledge.

### 2.6 Aggressive Input Truncation

File: `scripts/wiki/fetcher.py`

Classifier input is limited by:

```text
classify_chars = 512
```

Failure:

- Long PDRs, implementation reports, and technical plans lose context.
- The classifier may see a fragment but miss the project identity or durable decision.

### 2.7 Legacy Keyword Writer Still Exists

File: `scripts/memory_to_obsidian.py`

The old exporter uses `TOPIC_RULES`, hardcoded project links, truncation, and append-only topic pages.

Failure:

- It can still produce or preserve mechanically organized wiki content.
- It is not aligned with the generated wiki index.
- It can mislead agents because old pages may look authoritative while using keyword matching.

### 2.8 No Curation Quality Report

Current reports show counts, not knowledge quality.

Failure:

- A run can pass with no visible KASUMI or Manchuria topic.
- There is no warning when high-frequency project names remain unindexed.
- `UNCATEGORIZED_REVIEW` volume is not converted into actionable topic candidates.

## 3. Target Architecture

The fixed architecture should treat AI curation as a first-class stage.

Target flow:

```text
Stage 0: Fetch and privacy filtering
Stage 1: AI memory classification against active registry
Stage 2: AI topic discovery from uncategorized/recent/project-heavy memories
Stage 3: Topic registry update and candidate promotion
Stage 4: AI page synthesis
Stage 5: Index generation
Stage 6: Versioned vault publish
Stage 7: Curation quality report
```

The key shift:

```text
from: memory -> fixed topic label -> evidence dump
to:   memory -> AI curation -> evolving topic registry -> synthesized knowledge pages
```

## 4. Topic Registry

### 4.1 Storage

Use SQLite first because the wiki state already uses SQLite and cron can update it safely.

Add tables to `wiki_state.sqlite`:

```sql
CREATE TABLE topic_registry (
    topic_id TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    description TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    promoted_from_candidate_id TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE topic_candidate (
    candidate_id TEXT PRIMARY KEY,
    proposed_topic_id TEXT NOT NULL,
    display TEXT NOT NULL,
    description TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    evidence_ids_json TEXT NOT NULL,
    source_terms_json TEXT NOT NULL DEFAULT '[]',
    curator_reason TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    merge_target TEXT,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
```

Valid topic statuses:

- `active`
- `hidden`
- `retired`
- `merged`

Valid candidate actions:

- `promote`
- `merge`
- `keep_review`
- `ignore`

### 4.2 Compatibility

Initial migration should load current `TOPICS` into `topic_registry`.

During transition:

- `config.TOPICS` remains as a fallback seed.
- Classifier reads active topics from `topic_registry` when available.
- Page generator reads active topics from `topic_registry`.

This keeps daily cron backward compatible.

## 5. AI Topic Discovery Stage

### 5.1 Inputs

Discovery should consider:

- recent `UNCATEGORIZED_REVIEW`
- high-frequency terms in `UNCATEGORIZED_REVIEW`
- recent `NONE` rows with project-like names
- current active topics and aliases
- recent Daily logs with project path patterns such as `/home/lily/projects/...`
- repeated repo names, product names, system names, or game names

### 5.2 Candidate Prompt Contract

The AI curator should not merely classify. It should judge whether knowledge deserves a durable page.

Output schema:

```json
[
  {
    "proposed_topic_id": "Manchuria_Game",
    "display": "Manchuria Game",
    "description": "The Manchuria AI MUD game project, including design, implementation, runtime state, AI backends, TUI/CLI, REST angel integration, time system, and project decisions.",
    "aliases": ["Manchuria", "Manchuria: The AI MUD"],
    "evidence_ids": [123, 456],
    "source_terms": ["manchuria", "ai mud", "奉天城"],
    "recommended_action": "promote",
    "merge_target": null,
    "confidence": 0.93,
    "curator_reason": "Repeated project-level references with repository path, PDR, implementation milestones, runtime state, and design decisions."
  }
]
```

Rules:

- Prefer durable concepts/projects over transient tasks.
- Do not promote private, emotional, or relationship content.
- Prefer merge when a candidate is genuinely a subtopic of an existing topic.
- Promote when a topic has distinct purpose, lifecycle, files, decisions, or repeated agent use.
- Keep review when evidence is meaningful but insufficient.
- Ignore operational noise.

### 5.3 Initial Promotion Policy

For the first implementation, auto-promote only when:

- confidence >= `0.85`
- evidence count >= `5`
- at least two distinct days or two distinct agents are represented, unless the evidence contains a clear project path/repo name
- no privacy markers are present

Everything else goes to `30_GENERATED_INDEXES/Topic_Candidates.md`.

## 6. AI Page Synthesis

Replace evidence-only topic pages with synthesized pages.

Each generated page should contain:

```markdown
---
topic_id: "Manchuria_Game"
title: "Manchuria Game"
status: auto-generated
generated_at: "..."
evidence_count: 42
---

# Manchuria Game

## Current State

Concise current project status.

## Stable Facts

- Durable fact...

## Key Decisions

- Decision with source evidence...

## Recent Changes

- Recent update...

## Open Questions / Risks

- Open issue...

## Source Evidence

- Memory 123...
```

The page writer should be AI-driven and evidence-grounded.

Required safeguards:

- Every synthesized claim must cite one or more memory IDs in comments or a source section.
- The page must not invent files, statuses, or decisions.
- Low-confidence claims go to `Open Questions / Risks`, not `Stable Facts`.

## 7. Index Generation

The generated index should include:

- active topics grouped by category
- newly promoted topics
- candidate topics awaiting review
- topics with stale pages
- topics with high uncertainty
- last publish ID

Example sections:

```markdown
## Active Knowledge Topics
## Project Topics
## Research Topics
## Infrastructure Topics
## Newly Promoted
## Needs Review
## Retired / Merged
```

Index should be agent-oriented, not decorative.

## 8. Quality Gates

Daily report must include:

- number of active topics
- number of new candidates
- promoted candidates
- merged candidates
- ignored candidates
- `UNCATEGORIZED_REVIEW` count and top candidate clusters
- project/entity names detected but not indexed
- pages with no synthesis update
- pages with evidence older than N days
- private/sensitive skip counts

A run should be considered incomplete if:

- there are high-confidence topic candidates but no candidate page was written
- `UNCATEGORIZED_REVIEW` grows above threshold without review output
- project-like terms appear repeatedly but remain unindexed

## 9. Implementation Plan

### Phase 1: Registry Foundation

Files:

- `scripts/wiki/state.py`
- `scripts/wiki/config.py`
- `tests/test_wiki_pipeline.py`

Tasks:

- Add topic registry and candidate tables.
- Seed registry from existing `TOPICS`.
- Add read API: `load_active_topics()`.
- Update classifier/page generator to use registry when available.

Exit criteria:

- Existing tests pass.
- Existing topic pages still generate.
- No behavior change for daily cron unless registry exists.

### Phase 2: Discovery Queue

Files:

- new `scripts/wiki/topic_discovery.py`
- `scripts/wiki/run_pipeline.py`
- `tests/test_wiki_pipeline.py`

Tasks:

- Query recent `UNCATEGORIZED_REVIEW`.
- Build AI curator prompt.
- Parse candidate JSON.
- Persist candidates.
- Generate `30_GENERATED_INDEXES/Topic_Candidates.md`.

Exit criteria:

- KASUMI, Manchuria, WORDO-like samples produce candidates in tests.
- No auto-promotion yet unless policy permits.

### Phase 3: Candidate Promotion

Files:

- `scripts/wiki/topic_discovery.py`
- `scripts/wiki/state.py`
- `scripts/wiki/run_pipeline.py`

Tasks:

- Implement promotion policy.
- Insert promoted topics into `topic_registry`.
- Add aliases.
- Reclassify candidate evidence IDs against new topics.

Exit criteria:

- `Manchuria_Game` and `KASUMI_Software` can become active topics without editing Python `TOPICS`.
- Index lists newly promoted topics.

### Phase 4: AI Page Synthesis

Files:

- new `scripts/wiki/page_synthesizer.py`
- `scripts/wiki/page_generator.py`

Tasks:

- Replace raw evidence-only page body with AI synthesis.
- Keep evidence block for traceability.
- Add claim/evidence grounding checks.

Exit criteria:

- Generated topic page answers agent questions directly.
- Page still contains source memory IDs.
- Private/sensitive output filtering remains active.

### Phase 5: Quality Report and Cron Readiness

Files:

- `scripts/wiki/run_pipeline.py`
- `scripts/wiki/run_backfill_batches.py`
- tests

Tasks:

- Add curation quality report section.
- Include candidate stats and topic health.
- Ensure cron publishes generated topics, index, and candidate review page.

Exit criteria:

- Daily report states whether curation was successful, not just whether files were written.

## 10. Immediate Tactical Patch

Before the full architecture is complete, add a narrow interim patch:

- add registry/candidate infrastructure first
- run discovery over existing `UNCATEGORIZED_REVIEW`
- allow manually reviewed promotion for:
  - `KASUMI_Software`
  - `Manchuria_Game`
  - `WORDO`
  - `Sakura_Ref`

This should be treated as a bridge, not the final design.

## 11. Non-Goals

- Do not let AI write outside generated vault zones.
- Do not remove privacy filtering.
- Do not replace rollback/manifest publishing.
- Do not depend on keyword matching as the primary curation mechanism.
- Do not treat `UNCATEGORIZED_REVIEW` as an acceptable long-term sink.

## 12. Review Questions

1. Should high-confidence candidates auto-promote, or always require a generated review queue first?
2. Should project topics and concept topics have separate templates?
3. Should old `memory_to_obsidian.py` be disabled for topic pages once generated curation is active?
4. Should topic registry live only in SQLite, or also export a readable registry page into `00_SYSTEM/`?
5. What threshold should make daily cron report curation failure?

