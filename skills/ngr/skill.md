---
id: ngr
name: Nagare Workflow Manager
type: prompt
description: Create, list, or edit Nagare (NGR) multi-agent workflows. Usage: /ngr new <name> | /ngr list | /ngr edit <name>
---

Parse the first word of the user's command to determine which action to take:

- `new <name>` → Package a new NGR workflow (see below)
- `list` → List all available packaged NGR workflows
- `edit <name>` → Modify an existing packaged NGR workflow
- `run <name>` → Directly trigger a packaged workflow by name

---

## Action: new <name>

Run an **interactive packaging session** with the user to design and create a complete NGR workflow.

### Paths

- Workflow YAML: `/home/lily/projects/hashi/flow/workflows/library/<name>.yaml`
- Skill file: `/home/lily/projects/hashi/skills/<name>/skill.md`
- Workspace: `/home/lily/projects/hashi/flow/workspaces/<name>/`

### Step 1 — Understand the workflow

Ask the user questions one section at a time. Do not dump all questions at once. Cover:

1. **Purpose**: What does this workflow accomplish? What is the final deliverable?
2. **Steps**: What are the major stages? (e.g. research → draft → review → publish)
3. **Agents**: For each step, what kind of agent is needed? What is their role?
4. **Inputs**: What information must the user provide before the workflow starts? (pre-flight fields)
5. **Outputs**: What files or artifacts should be produced?
6. **Dependencies**: Which steps must happen before others? Can any run in parallel?

After each section, confirm your understanding and let the user correct or refine. Continue until the user confirms the design is complete.

### Step 2 — Draft the workflow YAML

Generate a Nagare workflow YAML based on the conversation. Structure:

```yaml
id: <name>
name: <Human-readable name>
description: <One-line description>
version: "1.0"

pre_flight:
  fields:
    - key: <field_key>
      label: <Human prompt>
      required: true|false

workers:
  - id: <agent_id>
    role: <role description>
    agent_md: /home/lily/projects/hashi/flow/agents/<role>/AGENT.md
    timeout: <seconds>

steps:
  - id: <step_id>
    agent: <agent_id>
    depends_on: []
    prompt: |
      <Task instructions. Use {pre_flight.field_key} for user inputs.
      Use {artifacts.<key>} to reference outputs from previous steps.>
    artifacts_produced:
      - key: <artifact_key>
        path: /home/lily/projects/hashi/flow/workspaces/<name>/<filename>
```

Show the draft YAML to the user and ask for feedback. Revise until approved.

### Step 3 — Create the files

Once the user approves the design:

1. Write the workflow YAML to `flow/workflows/library/<name>.yaml`
2. Create the workspace directory `flow/workspaces/<name>/`
3. Create the skill file at `skills/<name>/skill.md` using the template below
4. Confirm all files were created and show a summary

### Skill file template for packaged workflow

```markdown
---
id: <name>
name: <Human-readable name>
type: prompt
description: Run the <name> NGR workflow — <one-line description>
---

Run the **<name>** Nagare workflow.

## What this workflow does

<2-3 sentence description of the workflow's purpose and output>

## How to start

1. Collect the following inputs from the user if not already provided:
<list each pre_flight field as: - **<label>**: <description>>

2. Confirm the inputs with the user, then launch:
   ```
   cd /home/lily/projects/hashi && python flow/flow_trigger.py start <name> \
     --input "<field_key>=<value>" \
     --input "<field_key>=<value>"
   ```

3. Monitor the run:
   ```
   python flow/flow_trigger.py status <run_id>
   ```

4. When the workflow completes, check the workspace at:
   `/home/lily/projects/hashi/flow/workspaces/<name>/`
   and report the outputs to the user.

## On failure

If a step fails, check:
1. `flow/runs/<run_id>/logs/flow_runner.log` for step errors
2. `flow/runs/<run_id>/workers/<agent_id>/logs/` for agent-level errors

Report the error and ask the user whether to retry or abort.

## User's task

{prompt}
```

---

## Action: list

Show all packaged NGR workflows by scanning `/home/lily/projects/hashi/skills/` for skill files that trigger NGR workflows.

For each, display:
- Skill id
- Description
- Workflow YAML path (if exists)

---

## Action: edit <name>

1. Read the existing workflow YAML at `flow/workflows/library/<name>.yaml`
2. Read the existing skill file at `skills/<name>/skill.md`
3. Show the user what currently exists
4. Ask what they want to change
5. Make the changes interactively, same as the `new` packaging process
6. Write the updated files when approved

---

## Action: run <name>

Collect any required pre-flight inputs from the user (check the workflow YAML's `pre_flight.fields`), then launch:

```
cd /home/lily/projects/hashi && python flow/flow_trigger.py start <name> [--input key=value ...]
```

Monitor and report back.

---

## User's command

{prompt}
