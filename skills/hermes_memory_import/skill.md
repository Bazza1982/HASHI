---
id: hermes_memory_import
name: Hermes Memory Import
type: action
description: Import local Windows Hermes profile chat and memory files into Lily consolidated memory before wiki update
run: hermes_memory_import.py
---

Local-only action skill.

Purpose:
- Restore the existing Lily pre-wiki Hermes memory import entrypoint.
- Validate configured Hermes profile paths before importing.
- Run the existing standalone sidecar importer at `workspaces/lily/scripts/hermes_memory_import.py`.

Hard rules:
- Local execution only.
- No OpenRouter, DeepSeek, HASHI API relay, or external API use.
- Do not modify HASHI core.
- Do not write Obsidian vault.
- Only write through the existing importer into `consolidated_memory.sqlite` and `logs/hermes_memory_import.jsonl`.
- Report stdout/stderr clearly and return non-zero on failure.
