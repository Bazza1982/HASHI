---
name: memory-consolidation
description: Use when running or reviewing local nightly HASHI memory consolidation and embedding refresh across instances and agents.
---

# Memory Consolidation

Use the bundled `scripts/memory_consolidation.py` for deterministic scheduled runs.

Purpose:
- Run nightly memory consolidation across HASHI instances
- Fill BGE embeddings for newly consolidated records
- Return a complete report with per-instance and per-agent scan counts

Hard rules:
- Local execution only
- No OpenRouter
- No HASHI API relay
- No external API use for this task
- Do not omit zero-new agents from the report
- If there are errors, report them explicitly and do not hide partial failure
