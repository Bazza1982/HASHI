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

Lifecycle controls:

- `/skill` opens the catalog with per-agent enabled/disabled state, validation status, source, scope, resources, and structured Job references.
- `/skill install <directory>` validates and copies a local package into this HASHI project.
- `/skill link <directory>` validates and links a local development package without copying its source.
- `/skill enable|disable <name>` changes only the current agent's package state. Disabling a package with enabled Job references requires `--force`.
- `/skill validate [name]`, `/skill invalid`, `/skill find <text>`, and `/skill rescan` provide maintenance diagnostics.
- `/skill uninstall <name>` is available only for packages installed or linked through HASHI. Project packages are protected and can only be disabled.
- Uninstall is blocked while any structured `/jobs` definition references the package. Copied packages move to the local recovery area; linked packages are unlinked without deleting their source.

Install provenance is local runtime state. Packages without a HASHI install/link record are treated as protected project content, so a missing registry can never make repository Skills deletable.

Legacy underscore IDs such as `memory_consolidation` resolve to their kebab-case package during the transition, but new packages and new Jobs definitions must use kebab-case.
