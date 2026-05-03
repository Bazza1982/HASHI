# EXP Schema

This schema is intentionally file-based so EXP can be read by humans, agents,
tests, and future tools.

## Required files

Each EXP domain must include:

- `manifest.json`
- `EXP.md`

## Manifest fields

```json
{
  "id": "barry/office_desktop",
  "type": "exp",
  "version": 1,
  "owner": "barry",
  "domain": "office_desktop",
  "context": {
    "user": "Barry",
    "machine": "HASHI Windows desktop",
    "toolchain": ["use_computer", "windows_helper"],
    "applications": ["Word", "Excel", "PowerPoint"]
  },
  "playbooks": {
    "powerpoint": "playbooks/powerpoint.exp.md"
  },
  "failure_memory": "failures/failure_memory.jsonl",
  "validators": ["validators/office_validators.md"]
}
```

## EXP entry shape

Every durable EXP entry should record:

- Intent: when to use the EXP.
- Context: where it is known to work.
- Procedure: the concrete operational path.
- Evidence: screenshots, files, reports, or validation output.
- Recovery: what to do when the known failure appears again.
- Scope limit: where the EXP should not be assumed to transfer.

## Naming

Use lowercase domain names and concise playbook names:

- `word.exp.md`
- `excel.exp.md`
- `powerpoint.exp.md`
- `integrated_workflows.exp.md`

## Stability rules

- Prefer evidence-backed entries over guesses.
- Keep context explicit.
- Do not promote EXP into a generic skill unless it has proven transferable.
- Record failures as first-class memory, not as side notes.
- Use `training.md` before promoting a candidate EXP to stable.
