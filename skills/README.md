# HASHI Skills

HASHI Skills follow the [Agent Skills specification](https://agentskills.io/specification): one lowercase kebab-case directory containing an exact `SKILL.md` entrypoint.

```text
skills/<skill-name>/
├── SKILL.md          # required instructions
├── scripts/          # optional executable helpers
├── references/       # optional detailed documentation
└── assets/           # optional output resources
```

`SKILL.md` must start with YAML frontmatter containing the two required Agent
Skills fields:

```yaml
---
name: skill-name
description: Use when ...
---
```

Rules:

- `name` must equal the directory name and use lowercase kebab-case.
- `description` must say what the Skill does and when an agent should use it.
- Standard optional fields `license`, `compatibility`, `metadata`, and
  `allowed-tools` are accepted for package portability. HASHI treats them as
  metadata; its runtime permission policy remains authoritative.
- Non-standard frontmatter fields are rejected. In particular, legacy
  `type`, `run`, and `backend` fields do not restore action/toggle/routing behavior.
- The Markdown body contains the instructions loaded for `/skill <name> <request>`.
- Put deterministic helpers in `scripts/`; keep long optional material in `references/`.
- HASHI deliberately preserves its high-autonomy delegation defaults. Users can reduce permission or autonomy flags for a specific invocation.

Control-plane boundaries:

- `/skill` lists and applies instruction packages only.
- `/jobs` owns cron, heartbeat, nudge, and deterministic scheduled automation.
- `/debug`, recall state, and `/dream` are runtime controls, not Skill package types.
- `/EXP` remains an independent structured execution system.
- Native HER/Claw Skill discovery and execution are disabled; HASHI is the only Skill owner.

Legacy underscore IDs such as `memory_consolidation` resolve to their kebab-case package during the transition, but new packages and new Jobs definitions must use kebab-case.
