# HASHI Wiki Organiser — Redesign Plan

**Version:** 2.2-draft
**Author:** Lily (小蕾), reviewed by Ajiao (阿娇 @ HASHI2)
**Date:** 2026-05-03
**Status:** Design — ready to implement after daily Lily-cron conversion _(updated per Zelda review)_

---

## Table of Contents

1. [Why the Current System Fails](#1-why-the-current-system-fails)
2. [Architecture Overview](#2-architecture-overview)
3. [Stage 1: LLM Classifier](#3-stage-1-llm-classifier)
4. [Stage 2: Topic Page Generator](#4-stage-2-topic-page-generator)
5. [Stage 3: Daily Update and Weekly Digest](#5-stage-3-daily-update-and-weekly-digest)
6. [Stage 4: Agent Query Interface](#6-stage-4-agent-query-interface)
7. [Incremental Updates & Watermark Safety](#7-incremental-updates--watermark-safety)
8. [Privacy & Sensitivity Filter](#8-privacy--sensitivity-filter)
9. [Reliability: Retry, Token Cap, Atomic Writes](#9-reliability-retry-token-cap-atomic-writes)
10. [Module Structure](#10-module-structure)
11. [Migration from Current System](#11-migration-from-current-system)
12. [Token and Performance Estimate](#12-token-and-performance-estimate)
13. [Cron Integration](#13-cron-integration)
14. [Implementation Start Readiness](#14-implementation-start-readiness)
15. [Open Questions](#15-open-questions)

---

## 1. Why the Current System Fails

The current `wiki_organise.py` + `wiki_generate_review.py` pipeline has two failure modes that compound each other.

**Failure mode A — garbage-in at the retrieval layer.**
`get_topic_memories()` uses `LIKE` keyword matching. The word "hashi" appears in 80-90% of all memories because agents constantly refer to the HASHI system they run on. Every keyword list includes at least one token that bleeds across topics. The AI Review from 2026-04-25 confirmed this: Carbon_Accounting, Florence2, and Minato_Platform all received `[high]` severity ratings for topic mismatch because keyword retrieval fed wrong memories into those topics.

**Failure mode B — good LLM, wrong memories.**
`wiki_generate_review.py` already has solid synthesis prompts. The page quality problem is not the synthesis logic — it is that the input memories are semantically unrelated to the topic label. The LLM correctly synthesises what it is given, but what it is given is wrong.

**The fix is surgical:** replace keyword-retrieval with LLM-based classification, keep the synthesis stage largely intact, add watermark-based incremental processing.

---

## 2. Architecture Overview

```
consolidated_memory.sqlite
         │
         ▼
┌─────────────────────────────┐
│  Stage 0: Fetch & Filter    │  scripts/wiki/fetcher.py
│  - Pull new rows since      │
│    last_classified_id       │
│  - Filter noise (short,     │
│    system-only, FYI turns)  │
│  - Truncate to 512 chars    │
└────────────┬────────────────┘
             │  ~500-3000 memories/week (new only)
             ▼
┌─────────────────────────────┐
│  Stage 1: LLM Classifier    │  scripts/wiki/classifier.py
│  - Batch 30 memories/call   │
│  - Uses Lily's active       │
│    backend (claude-cli,     │
│    claude-sonnet-4-6)       │
│  - Returns topic_id[] per   │
│    memory (multi-label OK)  │
│  - Persists to              │
│    wiki_state.sqlite        │
└────────────┬────────────────┘
             │  classified assignments in DB
             ▼
┌─────────────────────────────┐
│  Stage 2: Page Generator    │  scripts/wiki/page_generator.py
│  - Per topic: fetch top-N   │
│    classified memories      │
│  - LLM synthesises          │
│    structured wiki page     │
│  - Writes Topics/*.md and   │
│    Projects/*.md to vault   │
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│  Stage 3: Daily/Weekly      │  scripts/wiki/digest.py
│  - Daily: run incremental   │
│    classification + changed │
│    page refresh             │
│  - Weekly: synthesise       │
│    Weekly/YYYY-Wnn digest   │
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│  Stage 4: Query Index       │  scripts/wiki/query_index.py
│  - Writes wiki_index.json   │
│  - Topic → page path,       │
│    summary, keywords, ts    │
│  - Agents call              │
│    wiki_lookup <topic>      │
└─────────────────────────────┘
             │
             ▼
      Obsidian Vault
      /mnt/c/Users/thene/Documents/lily_hashi_wiki/
```

Each stage is a standalone Python module with a `run(config, logger)` entry point. The orchestrator `scripts/wiki/run_pipeline.py` calls them in sequence, manages the watermark, and writes a run report. The production trigger is Lily's HASHI cron job, not system crontab and not an external API service.

---

## 3. Stage 1: LLM Classifier

### 3.1 Why LLM classification is necessary

"hashi" appears in ~90% of memories because all agents live inside HASHI. Keyword matching cannot distinguish "zelda is debugging the HASHI browser bridge" (→ HASHI_Architecture) from "zelda is writing a carbon accounting report using HASHI tools" (→ Carbon_Accounting). An LLM reading the actual content makes this distinction immediately.

### 3.2 Classification state database

A new SQLite file `workspaces/lily/wiki_state.sqlite` stores the classifier watermark and all assignments. Separate from `consolidated_memory.sqlite` to avoid coupling.

**Use a normalized assignment table instead of a JSON array column** (per Ajiao review — avoids complex `json_each` queries and makes per-topic lookups fast and correct):

```sql
-- Normalized: one row per (memory, topic) assignment
CREATE TABLE IF NOT EXISTS classification_assignment (
    consolidated_id  INTEGER NOT NULL,   -- FK to consolidated.id
    topic_id         TEXT    NOT NULL,   -- e.g. "HASHI_Architecture"
    confidence       REAL    NOT NULL DEFAULT 1.0,
    classified_at    TEXT    NOT NULL,
    classifier_model TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'ok',  -- 'ok' | 'failed' | 'redacted'
    PRIMARY KEY (consolidated_id, topic_id)
);

-- One row per memory processed (tracks watermark safety)
CREATE TABLE IF NOT EXISTS classification_run (
    consolidated_id  INTEGER PRIMARY KEY,
    agent_id         TEXT    NOT NULL,
    batch_id         TEXT    NOT NULL,   -- UUID for the batch call
    status           TEXT    NOT NULL,   -- 'ok' | 'failed' | 'skipped' | 'redacted'
    classified_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS run_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- run_state keys: last_classified_id (watermark), last_run_ts, last_digest_week, weekly_token_est
```

**Why `classification_run` matters:** The watermark (`last_classified_id`) only advances to the highest ID where all prior rows in `classification_run` have `status = 'ok'`. Failed batches are marked `failed` and retried next run — memories are never silently skipped.

### 3.3 Batching strategy

- Weekly new memories: ~2,000-3,000 (observed W12-W17 average: ~2,400/week)
- After noise filtering: ~1,500-2,000 classifiable per week
- Truncate each memory to 512 characters for classification
- **Batch size: 30 memories per LLM call**
- ~55-70 classification calls per week

### 3.4 Prompt design

```
System:
You are a knowledge classifier for a multi-agent AI system wiki.
Assign each memory snippet to the correct topic(s) from the HASHI wiki taxonomy.

Topics and their precise semantic scope (NOT keyword matching):
- HASHI_Architecture: HASHI multi-agent OS itself — agent lifecycle, hot-restart,
  orchestrator, bridge memory protocol, cron/scheduler internals, instance management
  (HASHI1/2/9), HChat transport. NOT generic software that runs inside HASHI.
- AI_Memory_Systems: How memories are stored/retrieved/embedded/consolidated.
  BGE-M3 embeddings, SQLite schema, consolidation pipeline, vector search.
  NOT just any mention of "memory".
- HASHI_Ops_Security: HASHI operations and security maintenance — system security
  scans, dependency update checks, chmod/file-permission reviews, firewall/port
  checks, Windows/WSL operational risk, and safe maintenance action planning.
  NOT the general HASHI orchestrator design itself.
- Nagare_Workflow: Nagare HITL workflow engine — JobQueue, Shimanto, workflow
  step design, checkpoint/approval flows. NOT generic task loops.
- Minato_Platform: Minato universal agentic AI OS — plugin-socket architecture,
  Veritas, KASUMI, AIPM integration. NOT HASHI itself.
- Dream_System: Nightly dream reflection — memory promotion, habit tracking,
  dream log generation.
- Obsidian_Wiki: Obsidian vault system — wiki page design, backlinks,
  memory_to_obsidian pipeline, vault structure.
- Carbon_Accounting: GHG protocol, emissions research, sustainability,
  scope 1/2/3 accounting. Zelda's research domain.
- Lily_Remote: Lily Remote / Hashi Remote app — remote control UI, P2P
  networking, mobile/desktop remote access.
- NONE: Pure infrastructure noise — startup pings, heartbeats, empty test messages,
  cross-instance health checks with no content. Cross-instance messages that CONTAIN
  a design decision or outcome are NOT NONE; classify them by content.
- UNCATEGORIZED_REVIEW: Content that is clearly significant but does not fit any
  defined topic. Use this when the memory is important (a decision, a design, a
  workflow) but the taxonomy has no matching label. Do NOT use NONE as a catch-all
  for things you are unsure about — prefer UNCATEGORIZED_REVIEW.

Rules:
1. Output only valid topic IDs from the list above. Any other string is an error.
2. Multi-topic only when content substantially addresses both topics.
3. "hashi" in content does NOT mean HASHI_Architecture. Read what it is about.
4. Low confidence (< 0.7) → use UNCATEGORIZED_REVIEW rather than forcing a topic.
5. The taxonomy is loaded from config.py at runtime — do not assume fixed topics.
6. Return only valid JSON, no commentary.

Output (strict JSON array):
[
  {"id": <consolidated_id>, "topics": ["TOPIC_ID", ...], "confidence": 0.95},
  ...
]

User:
Classify these {N} memories:
[{"id": 15001, "agent": "zelda", "content": "...512 chars..."}, ...]
```

### 3.5 Multi-topic handling

A memory assigned to two topics appears in both topic pages — correct behavior. Because assignments are normalized, multi-topic classification creates one `classification_assignment` row per `(consolidated_id, topic_id)` pair rather than storing a JSON array.

### 3.6 LLM backend — Lily-owned CLI backend, no remote API

**All LLM calls in the wiki pipeline go through Lily's own HASHI-owned CLI/local backend.** The production default is the same `claude-cli` route that Lily herself uses. There is no OpenRouter API, no DeepSeek API, no `httpx` HTTP client, and no separate billing account.

Concretely: `backend_client.py` invokes the `claude` CLI binary as a subprocess, passing a prompt file and collecting stdout as the response. This is identical to what the claude-cli adapter does for Lily's normal conversations.

```python
# backend_client.py — core call
result = subprocess.run(
    [CLAUDE_BIN, "--output-format", "json", "--print", prompt_text],
    capture_output=True, text=True, timeout=120
)
```

Benefits:
- **No extra API key** — uses Lily's existing claude authentication
- **Backend policy is safe by default** — if Lily's active backend is a remote API backend such as `openrouter-api` or `deepseek-api`, the wiki pipeline must stop and report instead of running.
- **CLI backend is switchable** — if Lily's active CLI backend changes to another approved CLI route such as `gemini-cli`, the wiki pipeline can follow after explicit config approval.
- **Token tracking** — claude-cli's native token output is parsed and logged to `wiki_tokens.jsonl`
- **No separate API billing route** — the job uses Lily's existing CLI authentication path, not a new API account or key

### 3.7 Token estimate — Stage 1

- Daily steady state depends on new memory volume. A typical day is expected to classify ~200-500 memories after filtering.
- 7-17 classification calls/day × ~6,000 tokens avg = ~42K-102K input tokens/day plus output.
- Billed through Lily's approved CLI backend route, not a separate API.
- **Token budget guard remains required** even when no remote API is used, because runaway automated CLI calls are still operationally expensive and slow.

---

## 4. Stage 2: Topic Page Generator

### 4.1 Changes from current system

The current `wiki_generate_review.py` has solid synthesis prompts. Changes needed:

1. **Input source:** Query `wiki_state.sqlite` via `classification_assignment.topic_id = ?`, join to `consolidated` for content — LLM-classified, not keyword-matched.
2. **Memory selection:** Top-N by recency, capped at 10 per agent, prioritise `memory_type = 'semantic'` over raw `'turn'` entries.
3. **Daily changed-page refresh, weekly full refresh** — normal daily runs update only changed pages; weekly maintenance can force a full rewrite so pages continue to represent current knowledge state.
4. **Confidence filter:** Only use classifications with `confidence >= 0.7`.
5. **Self-audit field** added to output schema:
   ```json
   {"scope_check": "Yes|Partial|No", "scope_note": "..."}
   ```
   Replaces the post-hoc AI Review step.

### 4.2 Memory selection query

Using the normalized `classification_assignment` table (no `json_each` complexity):

```sql
SELECT c.id, c.agent_id, c.memory_type, c.content, c.source_ts
FROM consolidated c
JOIN classification_assignment ca ON ca.consolidated_id = c.id
WHERE ca.topic_id = ?           -- e.g. 'HASHI_Architecture'
  AND ca.confidence >= 0.7
  AND ca.status = 'ok'
ORDER BY
    CASE c.memory_type WHEN 'semantic' THEN 0 WHEN 'episodic/md' THEN 1 ELSE 2 END,
    c.source_ts DESC
LIMIT 80
```

The `?` parameter is passed at query time from `config.py` — no hardcoded topic IDs in SQL.

### 4.3 Evidence tracking in generated pages

Every section in the generated wiki page must include a source evidence footer:

```markdown
<!-- Evidence: memory_ids=[15001, 15003, 15042] agents=[zelda, lily] -->
```

This is rendered as an Obsidian comment (invisible in reading mode, visible in source). It makes pages auditable — if a section looks wrong, the specific memories that produced it can be traced. Pages without evidence citations are not acceptable output from `page_generator.py`.

### 4.4 Token estimate — Stage 2

- Daily changed-page refresh should regenerate only topics/projects touched by newly classified memories.
- Weekly maintenance may still force a full topic/project rewrite for drift correction.
- Billed through Lily's approved CLI backend route, not a separate API.
- **Token budget:** usually small on daily runs; full weekly rewrite remains ~144K tokens.

---

## 5. Stage 3: Daily Update and Weekly Digest

### 5.1 Daily update design

The normal production run is daily. Lily's HASHI cron triggers the pipeline once per day after memory consolidation has completed. Production schedule:

```text
03:05  lily-memory-consolidation
04:05  lily-wiki-daily-update
```

The one-hour gap gives consolidation enough time to finish normal and moderately heavy runs before the wiki reads consolidated memory. The wiki pipeline must still verify that today's consolidation completed before it advances its own watermark.

Daily run behavior:

1. Fetch new consolidated memories since the safe watermark.
2. Apply privacy/noise filters.
3. Classify only new or previously failed rows.
4. Regenerate only changed topic/project pages.
5. Refresh `wiki_index.json`.
6. Write `wiki_organise_report_latest.md`.
7. Lily reads the report and sends a concise summary to the user.

The daily run normally refreshes the wiki once per day. On Saturdays, the same daily job also emits the weekly digest via `--weekly-if-saturday`; use one cron job rather than separate daily and weekly jobs so the wiki update does not run twice or race itself.

### 5.2 Weekly digest two-pass design

The current approach dumps 2,474 raw memories and shows only 28 to the LLM — an arbitrary cutoff that loses most of the week. Redesign uses two passes:

**Pass 1 (per topic, cheap):** For each topic with new memories this week, produce a 3-bullet summary via short LLM call (~800 tokens).

**Pass 2 (synthesis):** Feed all topic summaries (8-12 topics × 3 bullets) into a single digest synthesis call.

Scales cleanly regardless of raw memory volume.

### 5.3 Pass 1 prompt (per topic)

```
Topic: {topic_display_name} — {topic_desc}
Week: {week_str} ({start} to {end})
New memories this week ({count} total, showing {sample_n}):
{memory_lines}

Write exactly 3 bullet points (1-2 sentences each). Focus on:
- What actually changed or was decided (not background context)
- Be specific: name agents, components, outcomes
- If nothing significant happened, say so in one bullet

Return JSON: {"bullets": ["...", "...", "..."]}
```

### 5.4 Pass 2 prompt (digest synthesis)

```
Week: {week_str} | Total memories: {total_count} | Agents: {agent_list}

Per-topic summaries:
{topic_summaries}

Write weekly digest JSON:
{
  "summary": "2-3 sentence executive summary",
  "themes": [{"title": "...", "points": ["..."]}],  // max 4 themes
  "decisions": ["..."],        // concrete decisions made this week, max 6
  "highlights": ["..."],       // notable achievements, max 4
  "open_issues": ["..."],      // blockers carried to next week, max 5
  "cross_topic_links": ["..."] // how topics connected this week
}
```

### 5.5 Token budget — Stage 3

- 12 topic summaries + 1 synthesis = 13 calls × ~1,500 tokens avg
- **Token budget: ~20K tokens/week for Stage 3** — negligible

---

## 6. Stage 4: Agent Query Interface

### 6.1 Design goal

Agents currently have no fast path to look up current project state. The query interface provides: "give me the wiki page for topic X" — one bash command, no SQL, no vector search.

### 6.2 `wiki_index.json`

Written to `workspaces/lily/wiki_index.json` after each run:

```json
{
  "generated": "2026-05-03",
  "topics": {
    "HASHI_Architecture": {
      "display": "HASHI Architecture",
      "local_path": "workspaces/lily/wiki_pages/Topics/HASHI_Architecture.md",
      "vault_path": "/mnt/c/Users/thene/Documents/lily_hashi_wiki/Topics/HASHI_Architecture.md",
      "last_updated": "2026-05-03",
      "memory_count": 74,
      "summary": "Current state of HASHI browser bridge, agent lifecycle management..."
    }
  },
  "projects": { "...": "..." },
  "latest_digest": {
    "week": "2026-W18",
    "path": "workspaces/lily/wiki_pages/Weekly/2026-W18.md"
  }
}
```

### 6.3 `scripts/wiki/query.py` — agent CLI

```bash
# List all topics with summaries
python3 scripts/wiki/query.py --list

# Get full topic page (fuzzy match accepted: "hashi", "browser", "memory")
python3 scripts/wiki/query.py --topic HASHI_Architecture

# Truncate output for large pages (safe for agent context windows)
python3 scripts/wiki/query.py --topic HASHI_Architecture --max-chars 8000

# Machine-readable JSON output for stable agent parsing
python3 scripts/wiki/query.py --topic HASHI_Architecture --json

# Return only the file path (for agents that will read the file themselves)
python3 scripts/wiki/query.py --topic HASHI_Architecture --path-only

# Search across topic summaries (fuzzy, space-separated terms)
python3 scripts/wiki/query.py --search "browser bridge"

# Get latest weekly digest
python3 scripts/wiki/query.py --digest latest

# Short command alias (added to PATH or agent tool registry)
wiki_lookup browser bridge
wiki_lookup --topic nagare --max-chars 6000
```

**Fuzzy topic match:** `--topic` accepts partial names and aliases. `"hashi"` resolves to `HASHI_Architecture`; `"memory"` resolves to `AI_Memory_Systems`; `"nagare"` or `"workflow"` resolves to `Nagare_Workflow`. Aliases are defined in `config.py` alongside topic definitions.

**`--json` output schema:**
```json
{
  "topic_id": "HASHI_Architecture",
  "display": "HASHI Architecture",
  "last_updated": "2026-05-03",
  "memory_count": 74,
  "content": "...full markdown...",
  "truncated": false
}
```

Read-only. Reads `wiki_index.json` and `wiki_pages/` (Linux local mirror — faster than `/mnt/c`).

### 6.4 Local page mirror

The vault lives on Windows filesystem (`/mnt/c`). The pipeline also writes a mirror to `workspaces/lily/wiki_pages/` on Linux native filesystem. `query.py` uses the local mirror for low-latency reads.

---

## 7. Incremental Updates & Watermark Safety

### 7.1 Watermark

```
run_state key: "last_classified_id"   → safe high-water mark (see 7.2)
run_state key: "last_digest_week"     → "2026-W18"
run_state key: "last_daily_run_ts"    → ISO timestamp of last daily run
run_state key: "last_page_gen_ts"     → ISO timestamp of last page generation
run_state key: "weekly_token_est"     → accumulated estimated token use this calendar week
```

### 7.2 Watermark safety — no silent skips

`last_classified_id` is **not** simply `max(consolidated.id)` after a run. It is the highest ID for which all memories with `id ≤ N` have a `classification_run` row with `status = 'ok'`.

Algorithm:
```python
# After each batch completes:
# 1. Insert classification_run rows with status='ok' or status='failed'
# 2. Find the new safe watermark:
safe_watermark = db.scalar("""
    SELECT COALESCE(MIN(consolidated_id) - 1, last_committed_watermark)
    FROM classification_run
    WHERE status = 'failed'
      AND consolidated_id > ?
""", last_committed_watermark)
# 3. Only advance last_classified_id to safe_watermark
# 4. Failed batches are retried at the start of the next run
```

This guarantees no memory is permanently skipped due to a backend failure mid-run.

### 7.3 Classification — always incremental

Each run fetches only `consolidated.id > last_classified_id`. Failed rows from prior runs (status='failed' in `classification_run`) are also retried before advancing the watermark.

### 7.4 Page generation — daily changed pages, weekly full rewrite

Daily runs regenerate only changed topic/project pages. Weekly maintenance can force a full rewrite to refresh stale pages, correct topic drift, and generate the weekly digest.

### 7.5 Backfill on first run

```bash
python3 scripts/wiki/run_pipeline.py --backfill
```

Classifies all 15,820 existing memories: ~528 calls. This must be run manually with a higher explicit token cap and a backfill report.

### 7.6 Per-topic reclassification

```bash
python3 scripts/wiki/run_pipeline.py --reclassify-topic Carbon_Accounting --since 2026-04-01
```

Re-classifies one topic from a date without touching other topic assignments. Also supports `--reclassify-all` to redo all topics from scratch (needed when taxonomy changes substantially).

---

## 8. Privacy & Sensitivity Filter

This section is required before any memory reaches the LLM classifier or appears in a wiki page. The wiki is readable by all agents — personal and private memories must not become public knowledge pages.

### 8.1 Filter rules (applied in Stage 0 / fetcher.py)

| Memory characteristic | Action |
|---|---|
| `domain = 'identity'` or `domain = 'personal'` | Skip entirely — never classified or paged |
| `memory_type = 'private'` | Skip entirely |
| Content matches credential pattern (token, API key, password, path with sensitive keyword) | Redact content → store as `status='redacted'` in `classification_run`, skip page generation |
| Content is personal relationship / emotional context | Skip — add to `UNCATEGORIZED_REVIEW` audit log only, not paged |
| Agent is `temp` | Skip — temp agent memories are not durable wiki material |

### 8.2 Credential detection (simple regex, no LLM needed)

```python
SENSITIVE_PATTERNS = [
    r'[A-Za-z0-9_\-]{20,}:[A-Za-z0-9_\-]{30,}',  # token:secret style
    r'sk-[A-Za-z0-9]{32,}',                          # OpenAI/OpenRouter key
    r'eyJ[A-Za-z0-9+/]{40,}',                        # JWT
    r'(?i)(password|passwd|secret|api.?key)\s*[=:]\s*\S+',
]
```

Any memory matching a pattern has its content replaced with `[REDACTED: sensitive content detected]` before being stored in `classification_run`. The original content remains only in `consolidated_memory.sqlite` (the source DB), which is not public.

### 8.3 Personal memory domains

The `consolidated` table has a `domain` field. Domains that are excluded from wiki processing:
- `identity`, `personal`, `relationship`, `private`, `emotional`

This list is maintained in `config.py` as `PRIVATE_DOMAINS`.

### 8.4 UNCATEGORIZED_REVIEW audit output

Each week's run report includes a section listing:
- Count of memories assigned `UNCATEGORIZED_REVIEW`
- Top 5 content clusters within those memories (keyword frequency, no LLM needed)
- Suggestion: "These clusters may warrant new taxonomy topics"

This gives Lily visibility into emerging topics that the current taxonomy misses, without exposing private content.

---

## 9. Reliability: Retry, Token Cap, Atomic Writes

### 9.1 LLM call retry and backoff

All LLM calls in `backend_client.py` use exponential backoff:

```python
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds
# Retry on: CLI timeout, non-zero transient exit, malformed JSON, empty output
# Do NOT retry on: remote API backend selected, auth/config failure, privacy violation
```

If a batch fails after 3 retries, its `classification_run` rows are marked `status='failed'` and the run continues with the next batch. The run report lists all failed batches for manual inspection.

### 9.2 Token/time cap — auto-stop for runaway jobs

The production policy forbids remote metered API backends for this job. The cap is therefore operational rather than billing-first: prevent runaway CLI calls from monopolising Lily.

```python
if backend in {"openrouter-api", "deepseek-api"}:
    raise BackendPolicyError("Wiki pipeline must use Lily CLI/local backend, not remote API")

if weekly_token_est + estimated_call_tokens > WEEKLY_TOKEN_CAP:
    logger.error("Weekly wiki token cap reached. Stopping pipeline.")
    send_alert_to_lily("Wiki pipeline stopped: weekly token cap reached")
    raise TokenCapExceeded()

if run_elapsed_s > MAX_RUN_SECONDS:
    raise RunTimeoutExceeded()
```

`WEEKLY_TOKEN_CAP` and `MAX_RUN_SECONDS` are defined in `config.py` and overridable by explicit CLI flags. The token cap resets on Monday 00:00 (stored in `run_state`).

### 9.3 Atomic file writes

All file writes (wiki pages, `wiki_index.json`, run reports) use atomic write:

```python
def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(content, encoding='utf-8')
    tmp.replace(path)  # atomic on Linux (same filesystem)
```

This prevents half-written pages in Obsidian if the pipeline is interrupted mid-write. The `.tmp` file is cleaned up on next run start if it exists (indicating a prior crash).

---

## 10. Module Structure

```
scripts/wiki/
├── __init__.py
├── config.py              # Topic/project taxonomy, paths, model settings, aliases
│                          # PRIVATE_DOMAINS, WEEKLY_TOKEN_CAP, MAX_RUN_SECONDS, topic alias map
│                          # Replaces hardcoded TOPICS/PROJECTS in wiki_organise.py
│                          # Classifier prompt reads topic list from here at runtime
│
├── fetcher.py             # Stage 0: DB queries, noise filter, watermark reads
│   # fetch_new_memories(since_id) → list[MemoryRow]
│   # fetch_topic_memories(topic_id, limit) → list[MemoryRow]  (from classification_assignment)
│   # fetch_week_memories(week_str) → dict[topic_id, list[MemoryRow]]
│
├── privacy_filter.py      # Privacy & sensitivity screening (applied before classifier)
│   # filter_memories(memories) → (clean: list, redacted: list, skipped: list)
│   # Uses PRIVATE_DOMAINS and SENSITIVE_PATTERNS from config.py
│
├── classifier.py          # Stage 1: LLM batch classification
│   # classify_batch(memories) → list[ClassificationResult]
│   # run(config, logger, since_id=None) → ClassifyRunResult
│   # Handles failed batch retry and watermark-safe advancement
│
├── page_generator.py      # Stage 2: Topic/project synthesis
│   # Refactors ~70% of wiki_generate_review.py with new input source
│   # generate_topic_page(topic_id, memories) → str (with evidence comments)
│   # run(config, logger) → PageGenResult
│
├── digest.py              # Stage 3: Weekly digest (two-pass)
│   # summarise_topic_week(topic_id, memories) → list[str]
│   # generate_digest(week_str, summaries) → str
│   # run(config, logger, week_str=None) → DigestResult
│
├── query_index.py         # Stage 4: Write wiki_index.json + local mirror
│   # run(config, logger) → None
│   # atomic_write() used for all file output
│
├── query.py               # CLI for agents — pure read-only
│   # python3 scripts/wiki/query.py --topic X [--json] [--max-chars N] [--path-only]
│   # python3 scripts/wiki/query.py --search "terms" [--json]
│   # python3 scripts/wiki/query.py --list
│   # python3 scripts/wiki/query.py --digest latest
│   # Supports topic fuzzy match / aliases from config.py
│
├── backend_client.py      # Thin wrapper around Lily-approved CLI/local backend
│   # call(prompt_text, timeout=120) → str
│   # Invokes claude CLI binary as subprocess — same backend Lily uses
│   # Rejects remote API backends such as openrouter-api/deepseek-api
│   # CLAUDE_BIN read from global config (same as agents.json codex_cmd pattern)
│   # Retry: 3x with exponential backoff; raises TokenCapExceeded if cap hit
│   # Parses claude-cli JSON output for token counts
│   # Logs every call to wiki_tokens.jsonl — no separate API key needed
│
├── state.py               # wiki_state.sqlite read/write (normalized tables)
│   # get_watermark() → int  (safe watermark, not max id)
│   # advance_watermark(batch_results)  (only advances if contiguous ok)
│   # save_assignments(results)  (writes classification_assignment rows)
│   # save_run_rows(results)     (writes classification_run rows)
│
├── logger.py              # Structured logging (JSON lines + human log)
│                          # Each run writes wiki_run_{date}.log and wiki_run_{date}.jsonl
│
└── run_pipeline.py        # Orchestrator — replaces wiki_organise.py
    # python3 scripts/wiki/run_pipeline.py [--daily] [--weekly] [--backfill] [--dry-run]
    #   [--pages-only] [--digest-only] [--token-cap N] [--max-run-seconds N]
    #   [--reclassify-topic X --since DATE] [--reclassify-all]
    # Writes wiki_organise_report_latest.md (backward compat)
```

**State and data files:**

```
workspaces/lily/
├── wiki_state.sqlite          # Classification DB (new)
├── wiki_index.json            # Agent query index (new)
├── wiki_pages/                # Linux local mirror (new)
│   ├── Topics/
│   ├── Projects/
│   └── Weekly/
├── wiki_tokens.jsonl          # Per-call token log (new)
├── wiki_reports/              # Run reports (existing, keep)
└── wiki_organise_report_latest.md  # Compat symlink (keep)
```

---

## 11. Migration from Current System

### 11.1 Disposition of existing files

| File | Action | Reason |
|------|--------|--------|
| `scripts/wiki_generate_review.py` | Refactor → `scripts/wiki/page_generator.py` | Contains solid synthesis prompts — reuse ~70% |
| `scripts/wiki_organise.py` | Deprecate after 2-week parallel run | Keep as fallback during transition |
| `scripts/wiki_organise_cron.sh` | Deprecate | Production scheduling moves to Lily's HASHI cron jobs in `tasks.json` |
| `workspaces/lily/wiki_dump/` | Deprecate after migration | New system doesn't use intermediate JSON dumps |
| Obsidian vault structure | Unchanged | `/Topics/`, `/Projects/`, `/Weekly/` paths kept |

### 11.2 Lily HASHI cron update

Production scheduling uses Lily's existing HASHI `tasks.json` cron system. Do not use system crontab as the production scheduler.

Daily update cron:

```json
{
  "id": "lily-wiki-daily-update",
  "agent": "lily",
  "enabled": true,
  "schedule": "5 4 * * *",
  "action": "enqueue_prompt",
  "prompt": "每日 Wiki 增量更新任务！先确认今天 03:05 的 lily-memory-consolidation 已完成；若未完成，不得推进 wiki watermark，必须报告并停止。执行：cd /home/lily/projects/hashi && python3 scripts/wiki/run_pipeline.py --daily --weekly-if-saturday\\n\\n必须读取 wiki_organise_report_latest.md 后汇报：\\n- consolidation completion check\\n- 新分类 memories 数量\\n- changed topic/project pages\\n- low-confidence assignments\\n- UNCATEGORIZED_REVIEW clusters\\n- 失败 batches / retry 状态\\n- 周六 weekly digest 是否生成\\n- backend policy 是否确认未使用 OpenRouter/DeepSeek API\\n\\n禁止只粘 stdout；必须给出 Lily 的质量判断。"
}
```

The old weekly wiki cron is disabled in `tasks.json` during migration:

```json
{
  "id": "lily-wiki-organise-weekly",
  "agent": "lily",
  "enabled": false,
  "schedule": "10 2 * * 6",
  "action": "enqueue_prompt",
  "note": "Disabled: superseded by planned lily-wiki-daily-update at 04:05 after memory consolidation; old keyword pipeline kept only as fallback during migration"
}
```

### 11.3 Two-week parallel transition

**Week 1:** Build and backfill. Run `--dry-run` for page generation, compare quality against current wiki.

**Week 2:** Run both old and new daily for observation, but only publish pages from the new system after manual comparison. Compare 3 topics and 1 project manually. If new output quality ≥ old, disable the old weekly cron/system script.

**Week 3+:** Remove `wiki_organise.py`, `wiki_dump/`, update vault `_DESIGN.md`.

---

## 12. Token and Performance Estimate

### 12.1 LLM Backend — Lily's CLI/local backend, not remote API

**All LLM calls use Lily's own approved CLI/local backend.** Production default is `claude-cli` with Lily's configured Claude model. No OpenRouter key, no DeepSeek key, no httpx HTTP client, and no separate billing account. If Lily's active backend is remote API-backed, the wiki pipeline must refuse to run.

```python
# backend_client.py — how every prompt is sent
result = subprocess.run(
    [CLAUDE_BIN, "--output-format", "json", "--print", prompt_text],
    capture_output=True, text=True, timeout=120
)
```

This means:
- **No extra API key** required — uses Lily's existing Anthropic authentication
- **Same model, same quality** as Lily's normal conversations
- **CLI/local only** — `claude-cli`, approved `gemini-cli`, or explicitly approved local backend; never OpenRouter/DeepSeek for automated wiki work
- **Lily-owned automation** — the HASHI cron prompt is owned by Lily and she must read the run report before summarising

### 12.2 Daily steady-state token budget

| Stage | Calls | Input tokens | Output tokens |
|-------|-------|-------------|---------------|
| Stage 1: Classify ~200-500 new memories | 7-17 | ~42K-102K | ~5K-14K |
| Stage 2: Changed topic/project pages | 1-5 | ~10K-50K | ~2K-10K |
| Stage 4: Query index | 0 LLM | 0 | 0 |
| **Typical daily total** | **8-22** | **~52K-152K** | **~7K-24K** |

### 12.3 Weekly digest token budget

| Stage | Calls | Input tokens | Output tokens |
|-------|-------|-------------|---------------|
| Stage 3: Topic summaries (12 calls) | 12 | ~14,400 | ~4,800 |
| Stage 3: Digest synthesis | 1 | ~3,000 | ~2,000 |
| **Weekly digest add-on** | **13** | **~17K** | **~7K** |

**One-time backfill:** ~4.7M input tokens (15,820 memories ÷ 30/batch = 528 calls)

The token/time guards in §9.2 are the production safety mechanism. Remote API backends are blocked by policy, not cost-capped.

### 12.4 Wall time estimate

At ~5 seconds avg per call:
- Daily Stage 1: 7-17 calls (3 concurrent) → ~20-60 seconds
- Daily Stage 2: 1-5 page calls sequential → ~30-150 seconds
- Weekly Stage 3: 13 calls → ~2 minutes
- **Typical daily total:** ~1-4 minutes
- **Weekly digest add-on:** ~2 minutes

### 12.5 Token logging

Every LLM call logs to `wiki_tokens.jsonl`:

```json
{"ts": "2026-05-03T02:14:22", "stage": "classify", "call_n": 1, "batch_size": 30,
 "input_tokens": 5840, "output_tokens": 620, "model": "anthropic/claude-sonnet-4.6",
 "backend": "claude-cli"}
```

Run report includes total estimated tokens and backend policy status.

---

## 13. Cron Integration

Pipeline is self-contained. Lily's role after each run is to:
1. Read `wiki_organise_report_latest.md`
2. Flag any `confidence < 0.7` assignments
3. Report to 父亲 if ≥ 3 topics show systematic misclassification
4. Confirm backend policy: no OpenRouter/DeepSeek API used

```bash
# All run modes:
python3 scripts/wiki/run_pipeline.py --daily      # Normal daily run
python3 scripts/wiki/run_pipeline.py --daily --weekly  # Saturday weekly digest run
python3 scripts/wiki/run_pipeline.py --dry-run    # No writes
python3 scripts/wiki/run_pipeline.py --backfill   # Classify all history
python3 scripts/wiki/run_pipeline.py --pages-only # Skip classification
python3 scripts/wiki/run_pipeline.py --digest-only
python3 scripts/wiki/run_pipeline.py --reclassify-topic Carbon_Accounting --since 2026-04-01
```

---

## 14. Implementation Start Readiness

### 14.1 Milestone 1 implementation record

Status: implemented on 2026-05-04.

Implemented modules:

- `scripts/wiki/config.py` — paths, privacy domains, approved CLI backend policy, topic taxonomy seed.
- `scripts/wiki/state.py` — `wiki_state.sqlite` schema initialization for `classification_assignment`, `classification_run`, and `run_state`.
- `scripts/wiki/fetcher.py` — Stage 0 fetcher for new rows from `consolidated_memory.sqlite`, with private-domain skips and credential redaction.
- `scripts/wiki/run_pipeline.py` — dry-run orchestration, consolidation completion check, and no-LLM/no-vault report.

Validation record:

```text
python3 -m py_compile scripts/wiki/*.py tests/test_wiki_pipeline.py
pytest tests/test_wiki_pipeline.py -q
python3 scripts/wiki/run_pipeline.py --daily --dry-run --limit 20
```

Dry-run safety:

- No LLM classifier is called.
- No Obsidian vault pages are written.
- Dry-run reports write to `workspaces/lily/wiki_reports/wiki_dry_run_latest.md`, not the production `wiki_organise_report_latest.md`.
- `wiki_state.sqlite` schema initialization is the only persistent setup side effect.

### 14.2 Milestone 2 implementation record

Status: implemented on 2026-05-04.

Implemented modules:

- `scripts/wiki/backend_client.py` — resolves Lily's active backend from `workspaces/lily/state.json`, refuses remote API backends, and invokes only approved CLI/local backends.
- `scripts/wiki/classifier.py` — builds the topic taxonomy prompt, parses strict JSON classifier output, validates topic IDs, and supports deterministic mock dry-runs.
- `scripts/wiki/run_pipeline.py` — supports `--classify-dry-run`, `--mock-classifier`, and `--max-classify`.

Validation record:

```text
python3 -m py_compile scripts/wiki/*.py tests/test_wiki_pipeline.py
pytest tests/test_wiki_pipeline.py -q
python3 scripts/wiki/run_pipeline.py --daily --dry-run --classify-dry-run --mock-classifier --limit 20
python3 scripts/wiki/run_pipeline.py --daily --dry-run --classify-dry-run --limit 20 --max-classify 1
```

The real CLI smoke used Lily's active backend:

```text
Backend: claude-cli
Model: claude-sonnet-4-6
Assignments: 1
Topic Counts: AI_Memory_Systems: 1
```

Dry-run safety:

- Classifier assignments are parsed and reported but not written to `classification_assignment`.
- `last_classified_id` is not advanced.
- No Obsidian vault pages are written.
- Remote API backends such as `openrouter-api` and `deepseek-api` are refused before any call.

### 14.3 Milestone 3 implementation record

Status: implemented on 2026-05-04.

Implemented behavior:

- `scripts/wiki/state.py` can persist `classification_run` rows for `ok`, `skipped`, `redacted`, and `failed` outcomes.
- `classification_assignment` stores one row per `(consolidated_id, topic_id)` for successful classifier assignments.
- `last_classified_id` advances only across contiguous rows whose status is `ok`, `skipped`, or `redacted`.
- `failed` rows deliberately block watermark advancement so they can be retried.
- `scripts/wiki/run_pipeline.py` supports `--persist-classifications` for classifier-only persistence.

Validation record:

```text
python3 -m py_compile scripts/wiki/*.py tests/test_wiki_pipeline.py
pytest tests/test_wiki_pipeline.py -q
python3 scripts/wiki/run_pipeline.py --daily --dry-run --classify-dry-run --mock-classifier --limit 20
```

Persistence safety:

- Tests persist only to temporary SQLite databases.
- Live smoke remains dry-run only; no live `classification_assignment` rows have been written yet.
- No Obsidian vault pages are written.
- Page generation remains disabled until the classified state is reviewed.

### 14.4 Milestone 4 implementation record

Status: implemented on 2026-05-04.

Implemented behavior:

- `scripts/wiki/page_generator.py` reads classified memories from `wiki_state.sqlite` joined to `consolidated_memory.sqlite`.
- Topic page drafts are generated from persisted classification assignments.
- Draft output is written only to `workspaces/lily/wiki_pages_dry_run/Topics/`.
- `scripts/wiki/run_pipeline.py` supports `--pages-dry-run`.

Validation record:

```text
python3 -m py_compile scripts/wiki/*.py tests/test_wiki_pipeline.py
pytest tests/test_wiki_pipeline.py -q
python3 scripts/wiki/run_pipeline.py --daily --dry-run --classify-dry-run --mock-classifier --pages-dry-run --limit 20
```

Page dry-run safety:

- No Obsidian vault files are written.
- `wiki_pages_dry_run` pages include `status: dry-run` and a generated-draft comment.
- The live smoke produced no pages because live `classification_assignment` rows have not yet been written.
- Tests verify page generation using temporary classified state.

### 14.5 Milestone 5 live smoke record

Status: completed on 2026-05-04.

Live safety checkpoint:

```text
Backup created:
workspaces/lily/wiki_state.sqlite.bak-20260504-074816

Command:
python3 scripts/wiki/run_pipeline.py --daily --classify --persist-classifications --pages-dry-run --limit 20 --max-classify 3
```

Result:

```text
Backend: claude-cli
Model: claude-sonnet-4-6
Assignments: 3
Topic Counts:
- AI_Memory_Systems: 3
last_classified_id: 5
Page draft:
workspaces/lily/wiki_pages_dry_run/Topics/AI_Memory_Systems.md
```

Manual review:

- The three persisted assignments are all early memory-system / vector migration / memory backup items.
- Classification to `AI_Memory_Systems` is acceptable for this smoke test.
- The safe watermark stopped at `5` because later classifiable rows in the `--limit 20` window were intentionally not classified under `--max-classify 3`.
- No Obsidian vault files were written.
- The old DB backup remains available for rollback.

### 14.6 Milestone 6 expanded live smoke record

Status: completed on 2026-05-04.

Live safety checkpoint:

```text
Backup created:
workspaces/lily/wiki_state.sqlite.bak-20260504-080930

Command:
python3 scripts/wiki/run_pipeline.py --daily --classify --persist-classifications --pages-dry-run --limit 100 --max-classify 20
```

Result:

```text
Backend: claude-cli
Model: claude-sonnet-4-6
Assignments: 20
Topic Counts:
- AI_Memory_Systems: 4
- HASHI_Architecture: 11
- NONE: 5
last_classified_id: 36
Page drafts:
workspaces/lily/wiki_pages_dry_run/Topics/AI_Memory_Systems.md
workspaces/lily/wiki_pages_dry_run/Topics/HASHI_Architecture.md
```

Manual review:

- `AI_Memory_Systems` assignments remain correct for memory migration, memory sync, BGE-M3, vector search, and SQLite memory-system work.
- `NONE` correctly captured low-durable-value greetings, workspace-path checks, and generic memory-sync confirmations.
- Several security-scan and routine-maintenance memories were assigned to `HASHI_Architecture` with acceptable but lower confidence (`0.75`). This is not a data-integrity error, but it shows the taxonomy is too coarse for operational maintenance.
- Before a larger backfill, add a dedicated operations/security topic, for example `HASHI_Ops_Security`, covering system security scans, dependency update checks, file permissions, firewall/port review, Windows/WSL operational risk, and routine maintenance actions.
- No Obsidian vault files were written.
- The old DB backup remains available for rollback.

### 14.7 Milestone 7 taxonomy expansion record

Status: completed on 2026-05-04.

Change:

- Added `HASHI_Ops_Security` to the topic taxonomy.
- Scope: HASHI operations, routine maintenance, security scans, dependency update checks, file permissions, firewall/port review, Windows/WSL operational risk, and safe maintenance actions.
- Kept `HASHI_Architecture` focused on orchestrator, lifecycle, HChat, scheduler, gateway, instance topology, and architecture decisions.
- Updated the mock classifier so security/maintenance samples route to `HASHI_Ops_Security` in tests.

Live DB correction:

- The expanded live smoke had already classified `consolidated_id` 25, 26, and 27 as `HASHI_Architecture`.
- These rows are security-scan / maintenance-action memories, so the live `wiki_state.sqlite` assignments were updated to `HASHI_Ops_Security` after a fresh backup.
- No watermark rollback was required because only topic assignments changed; the rows remain processed.

### 14.8 Milestone 8 larger backfill quality record

Status: completed on 2026-05-04.

Live safety checkpoint:

```text
Backup created:
workspaces/lily/wiki_state.sqlite.bak-20260504-084100

Command:
python3 scripts/wiki/run_pipeline.py --daily --classify --persist-classifications --pages-dry-run --limit 300 --max-classify 60
```

Result:

```text
Backend: claude-cli
Model: claude-sonnet-4-6
Assignments: 60
Topic Counts:
- AI_Memory_Systems: 5
- Dream_System: 5
- HASHI_Architecture: 17
- HASHI_Ops_Security: 15
- NONE: 18
last_classified_id: 104
Page drafts:
workspaces/lily/wiki_pages_dry_run/Topics/AI_Memory_Systems.md
workspaces/lily/wiki_pages_dry_run/Topics/Dream_System.md
workspaces/lily/wiki_pages_dry_run/Topics/HASHI_Architecture.md
workspaces/lily/wiki_pages_dry_run/Topics/HASHI_Ops_Security.md
```

Quality review:

- `HASHI_Ops_Security` is working as intended. Startup conflicts, Windows Task Scheduler, exposed API keys, OpenRouter key rotation, WSL admin/UAC limits, file permission checks, and port/firewall review are now grouped separately from architecture.
- `AI_Memory_Systems` and `Dream_System` assignments are semantically clean in this batch.
- `HASHI_Architecture` remains useful for command implementation, agent lifecycle, backend/session bugs, `/reset`, `/retry`, OpenClaw command integration, and general instance/config design.
- `NONE` is mostly behaving as a low-durable-value sink for greetings, short path checks, push/commit confirmations, and relationship/persona content that should not become a wiki page.
- The quality pass exposed a privacy-filter gap: a few personal/relationship memories were excluded from pages by `NONE`, but they were not skipped at Stage 0 because the source domain was not always marked `personal`.
- Stage 0 now includes a narrow content-based private-pattern skip (`private_content_pattern`) for obvious relationship/intimacy phrases. This is intentionally conservative so ordinary technical conversations that merely use a familiar agent address are not over-filtered.
- Follow-up: watch for a possible future `Release_Deployment` or `Agent_Personas` topic, but do not add either yet. In this batch the evidence is not stable enough to justify more topic splits.
- No Obsidian vault files were written.
- The old DB backup remains available for rollback.

### 14.9 Milestone 9 Akane review response record

Status: completed on 2026-05-04.

Scope decision:

- Do not implement automatic emerging-topic discovery yet.
- Do not add `Anatta_Emotional` in this pass. New topic lifecycle review is deferred to a later weekly human/Lily review stage after the daily wiki workflow is stable.
- Keep the current taxonomy plus `HASHI_Ops_Security` for the next backfill pass.

Fixes applied from Akane review:

- Page generation now applies output-side privacy filtering through `sanitize_page_content()`. If generated page content matches `has_private_content()` or `has_sensitive_content()`, the page writes `[private content filtered]` instead of raw content.
- `HASHI_Architecture` now explicitly excludes security incidents, vulnerability scans, and operational restart procedures; those belong to `HASHI_Ops_Security` even when they mention core components.
- `fetch_new_memories()` now explicitly closes the read-only SQLite connection.
- `persist_classification_state()` now documents that classifiable rows beyond `--max-classify` are intentionally left unrecorded so they are refetched next run.

Still deferred:

- Topic lifecycle / emerging-topic review will be handled as a later weekly review workflow, not as an automatic Python clustering workflow.
- Obsidian vault publishing remains disabled until the dry-run pages receive at least one human review pass.

### 14.10 Milestone 10 manual backfill transport and quality record

Status: completed on 2026-05-04.

Problem found during larger manual backfill:

- `claude-cli` initially received the whole classifier prompt as a command-line argument.
- A larger batch hit the operating-system argument length limit:
  `OSError: [Errno 7] Argument list too long: 'claude'`.
- After moving the prompt to stdin, the next large batch reached the default `120s` timeout, which was too short for `--max-classify 250`.

Fixes:

- `claude-cli` prompts are now sent through stdin while keeping the same Lily-owned CLI backend policy.
- Wiki classifier timeout is now configurable through `WikiConfig.classifier_timeout_s`; the default is `600s`.
- No OpenRouter, DeepSeek, or other remote API backend is used.

Live safety checkpoint:

```text
Backup created:
workspaces/lily/wiki_state.sqlite.bak-20260504-100300

Command:
python3 scripts/wiki/run_pipeline.py --daily --classify --persist-classifications --pages-dry-run --limit 1000 --max-classify 250
```

Result:

```text
Backend: claude-cli
Model: claude-sonnet-4-6
Assignments: 250
Skipped: 262
Rows seen: 1000
Classifiable in fetch window: 738
last_classified_id: 424

New batch topic counts:
- AI_Memory_Systems: 27
- Carbon_Accounting: 1
- HASHI_Architecture: 80
- HASHI_Ops_Security: 81
- NONE: 9
- UNCATEGORIZED_REVIEW: 55
```

Remaining manual backfill after this run:

```text
Total consolidated rows: 16,261
Current watermark: 424
Raw remaining rows: 15,837
Classifiable remaining rows: 10,919
Skipped remaining rows: 4,904
Redacted remaining rows: 14
```

Quality review:

- `HASHI_Ops_Security` is routing credential exposure, git-history cleanup, release safety checks, and sensitive-info audits correctly.
- `AI_Memory_Systems` remains semantically clean for memory injection, bridge memory, retrieval, and reset/fresh-context behavior.
- `HASHI_Architecture` remains broad but useful for HASHI release, packaging, command/runtime, and system design work.
- `NONE` is still mostly low-durable-value content such as language correction, skill-context wrapper text, and interaction-quality complaints.
- `UNCATEGORIZED_REVIEW` is meaningful rather than random noise. The inspected examples are mostly AIPM, KASUMI, WORDO, milestone planning, and project-delivery work. Per Zelda's decision, these are kept as weekly review evidence rather than automatically promoted into new Python-defined topics.
- No Obsidian vault files were written.

### 14.11 Milestone 11 backfill efficiency record

Status: completed on 2026-05-04.

Efficiency problem found:

- One `--max-classify 250` run produced `249` classifier assignments.
- Because the missing memory id was not represented in `classification_run`, the conservative watermark stopped before the missing row.
- Later rows in the same fetch window had already been classified, but the next run would still refetch them because the watermark had not crossed the gap.

Fixes:

- Missing classifier assignments are now recorded as `failed` rows in `classification_run`, making the gap explicit and retryable.
- Future runs now skip already completed rows after the current watermark (`ok`, `skipped`, `redacted`) and only retry gaps or new rows.
- Failed rows are not skipped; they remain eligible for retry.

Validation:

```text
python3 -m py_compile scripts/wiki/*.py tests/test_wiki_pipeline.py
pytest tests/test_wiki_pipeline.py tests/contract/test_release_readiness_contract.py -q
# 22 passed
git diff --check
# passed
```

Live efficiency checkpoint:

```text
Backup created:
workspaces/lily/wiki_state.sqlite.bak-20260504-110200

Command:
python3 scripts/wiki/run_pipeline.py --daily --classify --persist-classifications --pages-dry-run --limit 1000 --max-classify 250

Elapsed:
239.71 seconds
```

Result:

```text
Assignments: 250
Rows seen after completed-row filtering: 407
Classifiable in fetch window: 400
Skipped: 7
last_classified_id: 1131

Topic counts:
- AI_Memory_Systems: 1
- HASHI_Architecture: 56
- HASHI_Ops_Security: 52
- Minato_Platform: 14
- NONE: 38
- Nagare_Workflow: 89
```

Remaining manual backfill after this run:

```text
Current watermark: 1131
Raw remaining rows: 15,130
Classifiable remaining rows: 10,420
Skipped remaining rows: 4,696
Redacted remaining rows: 14
```

Current best configuration:

- Manual backfill runner: `--max-classify 250` with no explicit `--limit`
- Classifier timeout: `600s`
- Daily steady-state cron: `--max-classify 100` with no explicit `--limit`
- Keep Obsidian vault publishing disabled until dry-run pages are reviewed.

### 14.12 Milestone 12 resumable manual backfill runner

Status: implemented on 2026-05-04.

Purpose:

- Run the manual historical backfill batch-by-batch without requiring an operator to launch each batch.
- Keep the same safe pipeline path: Python orchestration, Lily-owned CLI backend, SQLite watermark, page dry-run only.
- Make progress inspectable while the runner is active.
- Resume safely after interruption by relying on `wiki_state.sqlite`, completed-run filtering, and failed-row retry semantics.

Command:

```text
python3 scripts/wiki/run_backfill_batches.py --batches 42 --max-classify 250
```

Progress files:

```text
workspaces/lily/wiki_reports/wiki_backfill_progress_latest.json
workspaces/lily/wiki_reports/wiki_backfill_progress.jsonl
workspaces/lily/wiki_reports/wiki_backfill_runner.pid
workspaces/lily/wiki_reports/wiki_backfill_runner.lock
```

Operational behavior:

- Each batch runs `scripts/wiki/run_pipeline.py --daily --classify --persist-classifications --pages-dry-run --max-classify 250`.
- Historical backfill intentionally omits `--limit`; otherwise completed-row windows after a watermark gap can cause empty batches while later unprocessed rows still exist.
- A lock file prevents duplicate runners.
- Each batch writes one JSON progress event with elapsed time, return code, watermark before/after, and remaining classifiable rows.
- If a batch fails, the runner stops and leaves progress evidence for review.
- If the process is interrupted, re-running the same command resumes from existing persisted state; already completed rows after a watermark gap are filtered out.

### 14.13 Remaining implementation boundaries

The plan is ready to start once these implementation boundaries are accepted:

- Memory consolidation is scheduled at 03:05 and owned by Lily's HASHI cron job (`lily-memory-consolidation`).
- Daily production update is owned by Lily's HASHI cron job (`lily-wiki-daily-update`) at 04:05, after consolidation.
- Weekly digest is emitted by the same daily job on Saturday via `--weekly-if-saturday`, not a separate cron.
- The wiki pipeline checks today's consolidation completion before advancing its watermark.
- No system crontab is used for production scheduling.
- No OpenRouter API or DeepSeek API is used for automated wiki generation.
- Backend client refuses remote API backends and reports the policy block to Lily.
- The first implementation milestone is schema + dry-run only:
  - create `scripts/wiki/config.py`,
  - create `scripts/wiki/state.py`,
  - create `scripts/wiki/fetcher.py`,
  - run privacy/noise filtering,
  - create `wiki_state.sqlite`,
  - no Obsidian page writes yet.
- The second milestone is classifier dry-run:
  - classify a small sample,
  - validate topic assignments,
  - inspect `UNCATEGORIZED_REVIEW`,
  - keep old wiki output untouched.

---

## 15. Open Questions

1. **LLM backend confirmed:** All pipeline LLM calls use Lily-owned CLI/local backend. Production default is `claude-cli`. OpenRouter API and DeepSeek API are explicitly blocked for automated wiki runs. `CLAUDE_BIN` path is read from HASHI global config at runtime.

2. **Topic taxonomy expansion:** `HASHI_Ops_Security` has been added for operations/security maintenance. Another likely future topic is "Agent Personas" for character/identity work if those memories should become wiki material rather than `NONE` or private-domain skips.

3. **Confidence threshold:** Validate `confidence >= 0.7` during backfill pass. If a topic consistently gets 70%+ low-confidence, the taxonomy definition needs refinement, not the threshold.

4. **`wiki_state.sqlite` backup:** This DB is the source of truth for all topic assignments. Confirm it is included in any backup policy for `workspaces/lily/`.

5. **Taxonomy language:** Topic descriptions in the classifier prompt are English for precision. Page synthesis prompts remain Chinese-primary. This split is intentional.
