---
id: msn
name: Minato · Shimanto · Nagare Browser
type: prompt
description: Browse the M/S/N workflow hierarchy. Usage: /minato | /shimanto [minato] | /nagare [shimanto] [minato] | /nagare --minato [minato]
---

Parse the user's command and run the appropriate registry query.

## Commands

### /minato
List all Minato (top-level project domains).

Run:
```bash
cd /home/lily/projects/hashi && .venv/bin/python3 flow/flow_registry.py minato
```

### /shimanto [minato_slug]
List all Shimanto under the named Minato.

Run:
```bash
cd /home/lily/projects/hashi && .venv/bin/python3 flow/flow_registry.py shimanto <minato_slug>
```

If the user didn't provide a minato_slug, first run `/minato` to show available options and ask which one.

### /nagare [shimanto_slug] [minato_slug]
List all Nagare under a specific Shimanto.

Run:
```bash
cd /home/lily/projects/hashi && .venv/bin/python3 flow/flow_registry.py nagare <shimanto_slug> <minato_slug>
```

### /nagare --minato [minato_slug]   (or: /nagare [minato_slug] with no shimanto)
List all Nagare under a Minato, grouped by Shimanto.

Run:
```bash
cd /home/lily/projects/hashi && .venv/bin/python3 flow/flow_registry.py nagare --minato <minato_slug>
```

## Output

Display the command output directly to the user. Do not add commentary unless there are errors or the output is empty — in that case explain what's missing and how to add new entries.

## Slug resolution

Directory slugs use kebab-case (e.g. `ai-consulting`, `workflow-work`).
If the user types a human-readable name (e.g. "AI Consulting"), convert to kebab-case before passing to the script.

## User's command

{prompt}
