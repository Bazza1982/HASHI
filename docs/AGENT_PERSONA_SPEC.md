# HASHI Agent Persona Specification

This document defines the recommended format for an agent persona file referenced by
`system_md` in `agents.json`.

## Scope

An agent persona file is the agent's human-authored identity and communication profile.
It may contain additional system instructions after the persona block, but the
`[persona]` block is the stable, machine-detectable summary that should appear first.

## Required structure

Every new agent persona file should begin with exactly one block:

```text
[persona]
agent name <display name>; <role>; <tone>; <audience/addressing>; <language>; Emoji <emoji>
[persona_end]
```

Rules:

1. Use the literal markers `[persona]` and `[persona_end]` on their own lines.
2. Put the concise summary between the markers, normally one line.
3. Include the agent name, role or purpose, tone, preferred way to address the user,
   default language, and an optional signature emoji.
4. Keep the summary factual, concise, and safe to inject into runtime context.
5. Do not put secrets, API keys, private credentials, or long conversation history in
   the persona block.
6. Use one persona block per file. Put detailed identity, workflow, formatting, and
   safety rules after `[persona_end]`.
7. The file path is configured by the agent's `system_md` field; both `agent.md`
   and `AGENT.md` are valid filenames.

## Recommended extended sections

After the persona block, authors may add:

- `# Identity` — name, role, background, and relationship to the user.
- `# Voice and tone` — language, formality, warmth, and stylistic preferences.
- `# Responsibilities` — tasks the agent is expected to perform.
- `# Boundaries` — privacy, safety, approval, and escalation rules.
- `# Output format` — Markdown, headings, tables, emojis, or other presentation rules.

These headings are recommendations, not additional runtime schema requirements.

## Minimal example

See [`../examples/agent.md.sample`](../examples/agent.md.sample).

## Configuration example

The persona file is selected from the agent entry in `agents.json`:

```json
{
  "name": "researcher",
  "display_name": "Research Assistant",
  "type": "flex",
  "workspace_dir": "workspaces/researcher",
  "system_md": "workspaces/researcher/AGENT.md"
}
```

The `system_md` path must point to the actual persona file deployed for that agent.