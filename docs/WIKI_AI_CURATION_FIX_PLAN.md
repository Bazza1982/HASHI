# Wiki AI Curation Fix Plan

Status: draft - architecture direction accepted, implementation blocked on curation governance and publish safety
Owner: HASHI wiki pipeline
Created: 2026-05-04
Last reviewed: 2026-05-04 by ajiao@HASHI2

## 1. Problem Statement

The current wiki pipeline is not yet an AI-maintained knowledge curation system. It is an LLM-assisted fixed-label classification system.

This distinction matters because the wiki is meant to serve agents as a fast knowledge access layer. Agents need the wiki to surface durable projects, concepts, decisions, current state, and open questions. The current system can only publish pages for topics that already exist in a hardcoded taxonomy.

Observed failure examples:

- `KASUMI` exists as clear software/project knowledge but is folded into `Minato_Platform`.
- `Manchuria` exists as a clear game project but is only discoverable through Daily logs.
- `WORDO`, `AIPM`, `Nexcel`, `Zotero`, `Deep Dive Audio Paper`, and Sakura Ref-style work appear in `UNCATEGORIZED_REVIEW` rather than becoming first-class wiki topics.

The underlying failure is architectural: there is no topic discovery, topic promotion, AI page synthesis, AI topic governance, or claim-level knowledge validation loop.

This plan must not be implemented as another fixed taxonomy system with better labels. The fixed code path must only provide safety rails, storage, auditability, and rollback. The AI curator must make the knowledge judgement: what exists, what matters, what should merge, what should split, what has changed, what is uncertain, and what should be retired.

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

### 2.9 Discovery Still Depends on Existing Failure Buckets

A discovery process that only scans `UNCATEGORIZED_REVIEW` repeats the old failure: a memory must first be mishandled before it can be considered for new knowledge.

Failure:

- Clear topics hidden under broad existing topics may never be discovered.
- Early project documents can be classified into a nearby topic and then disappear from novelty review.
- The system becomes a dynamic taxonomy, not an autonomous knowledge curator.

### 2.10 Promotion Can Become Threshold-Driven

Thresholds such as evidence count, confidence, distinct days, or distinct agents are useful safety gates. They must not become the actual curation logic.

Failure:

- A single strong PDR or implementation plan can deserve a topic even if it appears once.
- A repeated noisy phrase can pass thresholds while lacking durable knowledge value.
- AI rationale becomes decorative if fixed thresholds decide promotion.

### 2.11 Publish Safety Is Not Yet a Hard Gate

The current publisher has manifest and rollback support, but the plan must require a staging-first publish gate for AI-generated content.

Failure:

- AI-generated text could reach the vault before privacy or quality checks.
- A failed LLM or malformed JSON run could still leave partial pages.
- Cron overlap could publish inconsistent page/index pairs.

## 3. Target Architecture

The fixed architecture should treat AI curation as a first-class stage.

Target flow:

```text
Stage 0: Fetch and privacy filtering
Stage 1: AI memory classification against active registry
Stage 2: AI novelty scan across recent and periodic full-library samples
Stage 3: AI topic discovery and topic governance review
Stage 4: Candidate promotion, merge, split, retire, or keep-review
Stage 5: AI claim extraction and page synthesis
Stage 6: Claim/evidence validation and privacy scan
Stage 7: Index generation
Stage 8: Staging-first versioned vault publish
Stage 9: Curation quality report
```

The key shift:

```text
from: memory -> fixed topic label -> evidence dump
to:   memory -> AI curation -> evolving topic registry -> synthesized knowledge pages
```

## 3.1 Core Design Principles

The implementation must follow these rules:

- AI judgement is primary for knowledge creation and maintenance.
- Fixed logic is allowed only for safety, persistence, audit, deduplication, rate limiting, and rollback.
- Thresholds do not decide truth or topic worth. They trigger review, block unsafe automation, or prioritize queues.
- Every AI-generated page is staged before publication.
- Every durable claim has structured evidence.
- Old keyword topic writing must be disabled once AI curation is active.
- The system must support merge, split, rename, retire, and uncertainty as first-class operations.

## 4. Topic Registry

### 4.1 Storage

Use SQLite first because the wiki state already uses SQLite and cron can update it safely.

Add tables to `wiki_state.sqlite`:

```sql
CREATE TABLE topic_registry (
    topic_id TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    description TEXT NOT NULL,
    topic_type TEXT NOT NULL,
    owner_domain TEXT NOT NULL DEFAULT '',
    canonical_page_path TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    privacy_level TEXT NOT NULL DEFAULT 'internal',
    quality_score REAL,
    uncertainty_score REAL,
    last_synthesized_at TEXT,
    human_locked INTEGER NOT NULL DEFAULT 0,
    ai_mutable INTEGER NOT NULL DEFAULT 1,
    merge_lineage_json TEXT NOT NULL DEFAULT '[]',
    split_lineage_json TEXT NOT NULL DEFAULT '[]',
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
    topic_type TEXT NOT NULL,
    owner_domain TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    evidence_ids_json TEXT NOT NULL,
    source_terms_json TEXT NOT NULL DEFAULT '[]',
    curator_reason TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    merge_target TEXT,
    confidence REAL NOT NULL,
    quality_score REAL,
    uncertainty_score REAL,
    privacy_level TEXT NOT NULL DEFAULT 'internal',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE topic_claim (
    claim_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    section TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(topic_id) REFERENCES topic_registry(topic_id)
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
- `split`
- `retire`
- `keep_review`
- `ignore`

Valid topic types:

- `project`
- `concept`
- `system`
- `research`
- `game`
- `workflow`
- `tool`
- `person_safe`
- `archive`

Valid privacy levels:

- `public_internal`
- `internal`
- `sensitive`
- `private_blocked`

### 4.2 Compatibility

Initial migration should load current `TOPICS` into `topic_registry`.

During transition:

- `config.TOPICS` remains as a fallback seed.
- Classifier reads active topics from `topic_registry` when available.
- Page generator reads active topics from `topic_registry`.

This keeps daily cron backward compatible.

Compatibility is temporary. `config.TOPICS` must not remain the long-term source of truth. After registry seeding and tests pass, `config.TOPICS` should become a seed fixture only, not the runtime taxonomy.

### 4.3 Topic Governance Operations

The registry must support these AI-curated operations:

- Create a new topic.
- Rename a topic while preserving aliases.
- Merge one topic into another and record lineage.
- Split one topic into multiple child topics and record lineage.
- Retire stale topics without deleting their evidence.
- Lock a topic against AI mutation when human review requires stability.
- Mark topics as uncertain when evidence is weak or contradictory.
- Escalate privacy-sensitive topics to `private_blocked`.

## 5. AI Topic Discovery Stage

### 5.1 Inputs

Discovery should consider:

- recent `UNCATEGORIZED_REVIEW`
- high-frequency terms in `UNCATEGORIZED_REVIEW`
- recent `NONE` rows with project-like names
- current active topics and aliases
- recent Daily logs with project path patterns such as `/home/lily/projects/...`
- repeated repo names, product names, system names, or game names
- all recent classifiable memories, even when already classified into an existing topic
- periodic historical samples across the full consolidated memory library
- existing topic pages whose evidence is too broad, stale, or internally mixed
- project directories and repo names only as hints, not as curation decisions

Discovery has two modes:

1. **Daily novelty scan:** run over recent memories, including classified and unclassified rows.
2. **Periodic full-library novelty scan:** sample or batch historical memory ranges to find topics missed by the original taxonomy.

The full-library scan is required. Without it, topics that were misclassified into broad buckets will never surface.

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
    "topic_type": "game",
    "quality_score": 0.87,
    "uncertainty_score": 0.18,
    "privacy_level": "internal",
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
- Explain why the candidate deserves a first-class page or why it should merge.
- Identify if the candidate is a split from an existing over-broad topic.
- Identify if an existing topic should be retired, renamed, or locked.

### 5.3 Initial Promotion Policy

Promotion is AI-rationale led. Thresholds are only safety gates and review triggers.

The curator may recommend promotion when the rationale shows durable knowledge value:

- distinct project, system, tool, game, research stream, workflow, or concept
- identifiable lifecycle, owner domain, files, decisions, or repeated agent use
- evidence that agents would need to retrieve later
- clear separation from existing topics

Fixed gates can block or escalate automation, but must not be the only reason to promote:

- confidence below `0.70` blocks auto-promotion
- privacy level `sensitive` requires review before publish
- privacy level `private_blocked` cannot publish
- malformed evidence IDs block promotion
- contradictory evidence raises `uncertainty_score` and sends to review

Examples:

- A single strong PDR with repo path and implementation decisions can be promoted.
- Ten shallow keyword mentions without durable knowledge can remain ignored.
- A candidate that overlaps an existing topic should be merged or split, not blindly promoted.

Everything else goes to `30_GENERATED_INDEXES/Topic_Candidates.md`.

### 5.4 AI Topic Governance Review

Discovery must also ask the curator:

- Which existing topics are too broad?
- Which topics should be split?
- Which topics should be merged?
- Which topics should be retired?
- Which topics need human lock?
- Which pages have high uncertainty or stale synthesis?

This prevents the registry from becoming append-only taxonomy sprawl.

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

### 6.1 Structured Claim Layer

Before writing Markdown, the synthesizer must output structured claims:

```json
[
  {
    "claim": "Manchuria is an AI MUD game project with a TUI/CLI starting point.",
    "section": "Stable Facts",
    "evidence_ids": [123, 456],
    "confidence": 0.86,
    "claim_type": "project_identity"
  }
]
```

Valid claim types:

- `project_identity`
- `current_state`
- `architecture`
- `decision`
- `implementation`
- `risk`
- `open_question`
- `historical_context`
- `status_change`

Markdown is rendered from accepted claims, not directly from free-form AI prose.

### 6.2 Claim Validation

Validation gates:

- every claim must have at least one evidence ID
- evidence IDs must exist in the consolidated memory database
- claims marked `Stable Facts` need confidence >= `0.75`
- claims with contradictory evidence move to `Open Questions / Risks`
- private/sensitive evidence cannot support a public/internal claim without redaction
- empty or generic claims are dropped

If claim validation fails, the topic page is not published. The candidate/page goes to staging review.

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
## Game Topics
## Tool Topics
## Newly Promoted
## Needs Review
## Retired / Merged
```

Index should be agent-oriented, not decorative.

Index grouping must be derived from `topic_type` in the registry, not from another hardcoded category list. Display order may be configured, but topic membership must remain registry-driven.

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
- full-library novelty scan status
- claim validation failures
- publish gate status
- stale lock / cron lock status

A run should be considered incomplete if:

- there are high-confidence topic candidates but no candidate page was written
- `UNCATEGORIZED_REVIEW` grows above threshold without review output
- project-like terms appear repeatedly but remain unindexed
- full-library novelty scan is overdue
- AI output fails JSON parsing
- claim validation fails for any page selected for publish
- privacy scan fails
- staging manifest is missing or inconsistent

## 8.1 Vault Publish Hard Gates

AI-generated wiki content must use staging-first publish:

```text
AI output -> staging files -> parse/claim/privacy checks -> staging manifest -> publish -> latest manifest
```

Required gates before writing to the live vault:

- all AI JSON outputs parse successfully
- every staged page has a matching manifest entry
- privacy scan passes for each staged page
- claim validation passes for every synthesized page
- generated index links only to existing staged/live generated pages
- manifest records created, updated, deleted, unchanged, content hashes, and source evidence IDs
- rollback covers created, updated, and deleted files
- cron lock is acquired before publishing
- if any gate fails, no live vault write occurs

Cron requirements:

- one wiki pipeline run at a time
- lock has stale-lock recovery with explicit report entry
- LLM timeout, JSON parse failure, privacy failure, or quality failure stops publish
- failed publish leaves staging artifacts for review

## 8.2 Legacy Writer Decommission Gate

Once AI curation is enabled:

- `scripts/memory_to_obsidian.py` must not write `Topics/*.md`
- old keyword-derived topic pages must be treated as legacy archive or read-only historical material
- generated index must clearly point agents to `30_GENERATED_INDEXES/Wiki_Index.md`
- if the legacy exporter still runs for Daily/Agents/EmailReports, topic writing must be disabled by code or flag
- reports must warn if legacy topic pages were modified after AI curation activation

## 9. Implementation Plan

### Phase 1: Registry Foundation and Governance Schema

Files:

- `scripts/wiki/state.py`
- `scripts/wiki/config.py`
- `tests/test_wiki_pipeline.py`

Tasks:

- Add topic registry and candidate tables.
- Add topic claim table.
- Add topic type, canonical page path, privacy level, lineage, human lock, quality score, uncertainty score, and synthesis timestamp fields.
- Seed registry from existing `TOPICS`.
- Add read API: `load_active_topics()`.
- Update classifier/page generator to use registry when available.
- Make `config.TOPICS` a seed fallback, not the long-term runtime source of truth.

Exit criteria:

- Existing tests pass.
- Existing topic pages still generate.
- No behavior change for daily cron unless registry exists.
- Registry export can show active topics and governance metadata.

### Phase 2: AI Novelty Scan and Discovery Queue

Files:

- new `scripts/wiki/topic_discovery.py`
- `scripts/wiki/run_pipeline.py`
- `tests/test_wiki_pipeline.py`

Tasks:

- Query recent `UNCATEGORIZED_REVIEW`.
- Query recent classified memories as well.
- Add periodic full-library novelty scan mode.
- Build AI curator prompt.
- Parse candidate JSON.
- Persist candidates.
- Generate `30_GENERATED_INDEXES/Topic_Candidates.md`.
- Report candidate rationale, merge/split/retire recommendations, and uncertainty.

Exit criteria:

- KASUMI, Manchuria, WORDO-like samples produce candidates in tests.
- Topics hidden under broad existing labels can also produce candidates.
- No auto-promotion yet unless AI rationale plus safety gates permit.

### Phase 3: Candidate Promotion and Topic Governance

Files:

- `scripts/wiki/topic_discovery.py`
- `scripts/wiki/state.py`
- `scripts/wiki/run_pipeline.py`

Tasks:

- Implement AI-rationale-led promotion policy.
- Use thresholds only as safety gates.
- Insert promoted topics into `topic_registry`.
- Add aliases.
- Implement merge, split, retire, rename, lock, and keep-review actions.
- Reclassify candidate evidence IDs against new topics.

Exit criteria:

- `Manchuria_Game` and `KASUMI_Software` can become active topics without editing Python `TOPICS`.
- Index lists newly promoted topics.
- Merge/split lineage is recorded.
- Human locked topics cannot be mutated by AI.

### Phase 4: AI Claim Extraction and Page Synthesis

Files:

- new `scripts/wiki/page_synthesizer.py`
- `scripts/wiki/page_generator.py`

Tasks:

- Extract structured claims before Markdown rendering.
- Validate claims against evidence IDs.
- Replace raw evidence-only page body with claim-backed AI synthesis.
- Keep evidence block for traceability.
- Add claim/evidence grounding checks.

Exit criteria:

- Generated topic page answers agent questions directly.
- Page still contains source memory IDs.
- Private/sensitive output filtering remains active.
- A claim with missing evidence blocks publish.

### Phase 5: Staging Publish Gate, Quality Report, and Cron Readiness

Files:

- `scripts/wiki/run_pipeline.py`
- `scripts/wiki/vault_publisher.py`
- `scripts/wiki/run_backfill_batches.py`
- tests

Tasks:

- Add staging directory and staging manifest.
- Add cron lock.
- Extend rollback to cover created, updated, and deleted generated files.
- Add curation quality report section.
- Include candidate stats and topic health.
- Ensure cron publishes generated topics, index, and candidate review page.
- Stop live publish on LLM, JSON, claim validation, privacy, staging manifest, or lock failure.

Exit criteria:

- Daily report states whether curation was successful, not just whether files were written.
- Failed curation does not update the live vault.
- Staging artifacts remain inspectable after failure.

### Phase 6: Legacy Topic Writer Disablement

Files:

- `scripts/memory_to_obsidian.py`
- cron/task configuration
- tests

Tasks:

- Disable `update_topic_pages()` unless explicitly run in legacy archive mode.
- Keep Daily, Agents, EmailReports if still needed.
- Add report warning when legacy topic writing is attempted.
- Document generated wiki as the authoritative agent knowledge entry point.

Exit criteria:

- Old keyword topic writer cannot modify `Topics/*.md` during normal runs.
- Agents have one authoritative generated topic index.

## 10. Immediate Tactical Patch

Before the full architecture is complete, add a narrow interim patch:

- add registry/candidate infrastructure first
- run discovery over existing `UNCATEGORIZED_REVIEW` and already-classified broad topics
- run an initial full-library novelty scan batch
- allow manually reviewed promotion for:
  - `KASUMI_Software`
  - `Manchuria_Game`
  - `WORDO`
  - `Sakura_Ref`
- disable legacy topic writes during the tactical patch

This should be treated as a bridge, not the final design.

## 11. Non-Goals

- Do not let AI write outside generated vault zones.
- Do not remove privacy filtering.
- Do not replace rollback/manifest publishing.
- Do not depend on keyword matching as the primary curation mechanism.
- Do not treat `UNCATEGORIZED_REVIEW` as an acceptable long-term sink.
- Do not let fixed thresholds decide knowledge worth.
- Do not keep `config.TOPICS` as the permanent taxonomy authority.
- Do not allow old keyword topic pages to compete with generated AI-curated pages.

## 12. Review Questions

1. Which topic governance actions should be allowed to auto-apply, and which require human review?
2. Should project, game, system, tool, workflow, and research topics have separate synthesis templates?
3. What cadence should the full-library novelty scan use?
4. Should topic registry live only in SQLite, or also export a readable registry page into `00_SYSTEM/`?
5. What quality score / uncertainty score should block publication?
6. Should old `memory_to_obsidian.py` stop writing `Topics/*.md` immediately, or only after Phase 2?
7. Which publish gate failures should page the user versus only write a report?
