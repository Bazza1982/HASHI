# HASHI EXP

EXP means context-specific expertise and experience.

An EXP is not a generic skill. It is learned operational knowledge that only
becomes reliable inside a specific context: a user, machine, toolchain,
template set, workflow, and evidence history.

Use EXP when an agent needs to recall how a task is done well in this HASHI
environment without changing HASHI's core program.

## Difference from skills

- Skill: generic capability that should transfer across users and projects.
- EXP: tailored knowledge learned over time for one context.

EXP can include:

- user preferences
- machine-specific behavior
- helper-tool recipes
- UI operation playbooks
- template fingerprints
- failure memory
- validation rules
- evidence from previous runs

## Layout

```text
exp/
  README.md
  loader.py
  schema.md
  <owner>/
    <domain>/
      manifest.json
      EXP.md
      playbooks/
      failures/
      validators/
      templates/
      evidence/
```

## Callable contract

EXP is callable through the lightweight loader:

```python
from exp.loader import ExpStore

store = ExpStore()
manifest = store.get_manifest("barry/office_desktop")
playbook = store.get_playbook("barry/office_desktop", "powerpoint")
```

The loader is read-only. It does not register commands, mutate HASHI runtime
state, or alter core orchestration behavior.

## Training

EXP should be trained, not merely written. See `training.md` for the standard
program for turning templates, examples, goals, practice, validators, and
failure memory into a stable EXP.

Future agents should also read `AGENT.md` before continuing EXP training or
handing over a final artefact. It contains trainer-side review rules and
mistakes that must not be repeated.
