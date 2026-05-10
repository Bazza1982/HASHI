# Soul Repository

The Soul Repository is the durable seed layer for specialized HASHI personas.
Seed files live in `agent_seeds/` and are preserved across normal agent resets.

## Deploy A Seed

Use `tools/deploy_soul.py` from the repository root:

```bash
python tools/deploy_soul.py zelda
```

This creates:

- `workspaces/<agent_id>/AGENT.md`
- a Flex agent entry in `agents.json`
- a `secrets.json` token key using `WORKBENCH_ONLY_NO_TOKEN` by default

Then restart HASHI so the new agent is loaded:

```text
/reboot
```

## Dry Run

Preview the deployment without writing files:

```bash
python tools/deploy_soul.py zelda --dry-run
```

## Common Options

```bash
python tools/deploy_soul.py samantha sam_test --display-name "Samantha Test" --emoji "🧡"
python tools/deploy_soul.py baymax --active
python tools/deploy_soul.py zelda --telegram-token "123456:ABC..."
```

If a Telegram token is not available, keep the default placeholder:

```text
WORKBENCH_ONLY_NO_TOKEN
```

## Safety Rules

- Existing `workspaces/<agent_id>/AGENT.md` files are not overwritten unless `--overwrite-agent-md` is passed.
- Existing `agents.json` entries are not replaced unless `--overwrite-agent` is passed.
- Existing `secrets.json` token values are preserved unless `--overwrite-secret` is passed.
- New deployments use `workspaces/<agent_id>/AGENT.md` as the `system_md` path.

## Agent Type

The deployment helper creates Flex agents by default:

- one bot
- one workspace
- switchable backend through `/backend`
- persisted backend/model state in `workspaces/<agent_id>/state.json`
