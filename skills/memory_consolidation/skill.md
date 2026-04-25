---
id: memory_consolidation
name: Memory Consolidation
type: action
description: Local nightly memory consolidation and embedding refresh
run: memory_consolidation.py
---

Local-only action skill.

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
