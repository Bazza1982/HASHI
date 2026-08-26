# HASHI Agent Persona Specification

This document defines the required HASHI PCM format for the exact lower-case
`agent.md` file in each Agent workspace.

## Scope

The document owns the Agent's human-authored identity, permanent system
instructions, and optional stable memory. HASHI rejects substantive unmarked text.

## Required structure

Every Agent requires exactly one Persona block and one System block. A Memory
block is optional:

```text
[persona]
agent name <display name>; <role>; <tone>; <audience/addressing>; <language>; Emoji <emoji>
[persona_end]

[sys]
Permanent operating, safety, workflow, and output instructions.
[sys_end]

[memory]
Optional stable facts or preferences that should persist.
[memory_end]
```

Rules:

1. Use the literal markers `[persona]` and `[persona_end]` on their own lines.
2. Put the concise summary between the markers, normally one line.
3. Include the agent name, role or purpose, tone, preferred way to address the user,
   default language, and an optional signature emoji.
4. Keep the summary factual, concise, and safe to inject into runtime context.
5. Do not put secrets, API keys, private credentials, or long conversation history in
   the persona block.
6. Use exactly one `[persona]` and one `[sys]` block, and at most one `[memory]` block.
7. Put all substantive content inside these recognised blocks. Duplicate, empty,
   mismatched, unclosed, or unmarked content invalidates the whole document.
8. The filename is always exactly `agent.md` in the configured workspace. Do not
   add `system_md` to new `agents.json` entries.

## Recommended extended sections

Inside the `[sys]` block, authors may add Markdown headings such as:

- `# Identity` — name, role, background, and relationship to the user.
- `# Voice and tone` — language, formality, warmth, and stylistic preferences.
- `# Responsibilities` — tasks the agent is expected to perform.
- `# Boundaries` — privacy, safety, approval, and escalation rules.
- `# Output format` — Markdown, headings, tables, emojis, or other presentation rules.

These headings are recommendations within the block, not additional block types.

## Minimal example

See [`../examples/agent.md.sample`](../examples/agent.md.sample).

## Configuration example

The PCM file is derived from `workspace_dir`:

```json
{
  "name": "researcher",
  "display_name": "Research Assistant",
  "type": "flex",
  "workspace_dir": "workspaces/researcher",
  "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.4"}],
  "active_backend": "codex-cli"
}
```

This entry requires `workspaces/researcher/agent.md`. Legacy `system_md` is accepted
only as one-time migration input and is removed after successful validation.
