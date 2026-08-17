---
name: hermes-memory-import
description: Use when validating or importing local Windows Hermes profile chat and memory files into Lily consolidated memory before a wiki update.
---

# Hermes Memory Import

Use the bundled `scripts/hermes_memory_import.py` for deterministic scheduled runs.

Purpose:
- Use the existing Lily pre-wiki Hermes memory import entrypoint.
- Validate configured Hermes profile paths before importing.
- Run the existing standalone sidecar importer at `workspaces/lily/scripts/hermes_memory_import.py`.

Hard rules:
- Local execution only.
- No OpenRouter, DeepSeek, HASHI API relay, or external API use.
- Do not modify HASHI core.
- Do not write Obsidian vault.
- Only write through the existing importer into `consolidated_memory.sqlite` and `logs/hermes_memory_import.jsonl`.
- Report stdout/stderr clearly and return non-zero on failure.
