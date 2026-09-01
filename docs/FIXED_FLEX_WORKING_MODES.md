# Fixed and Flex Working Modes

Status: **accepted product contract for HASHI v4.0.0-alpha.2 (2026-09-01)**.

HASHI exposes two Agent working modes: **Fixed** and **Flex**. Fixed is the
default when the selected backend can preserve a native session. Flex is the
explicit backend-switching mode.

## Keep the terms separate

| Layer | Current choices | What it controls |
|---|---|---|
| Configured Agent runtime | `type: "flex"` for normal Agents | One workspace, identity, backend manager, and command surface |
| Agent working mode | Fixed or Flex | Native-session continuation versus bridge-managed context and backend switching |
| HER execution mode | Direct, Strategic, or Planned | How much HER orchestration one task receives |
| Memory+ | On or Off | Optional compact continuity, independent of working mode |

Fixed does not restore the retired legacy `type: "fixed"` runtime. It is a
session-preserving working mode inside `FlexibleAgentRuntime`.

## Runtime contract

| Behavior | Fixed | Flex |
|---|---|---|
| Default | Yes, for a session-capable active backend | Yes, when the active backend is stateless |
| Context | Reuses the backend's native session and sends incremental turns once resume is available | HASHI assembles the applicable context for each request |
| Backend switching | `/backend` first asks to switch to Flex | `/backend` is available directly |
| Session requirement | Required | Not required |
| Persisted value | `agent_mode: "fixed"` | `agent_mode: "flex"` |

The session-capable backend set is owned by
`orchestrator.config.SESSION_MODE_BACKENDS`. It currently contains
`claude-cli`, `codex-cli`, `grok-cli`, and `her-v2`. Backend capability checks
still run when a user selects Fixed; a backend whose runtime capabilities
report no session support is rejected without changing state.

## Configuration

Normal Agent configuration keeps `type: "flex"` and may set `default_mode`:

```json
{
  "name": "zelda",
  "type": "flex",
  "workspace_dir": "workspaces/zelda",
  "allowed_backends": [
    {"engine": "codex-cli", "model": "gpt-5.4"}
  ],
  "active_backend": "codex-cli",
  "default_mode": "fixed"
}
```

Rules:

- `default_mode` accepts only `fixed` or `flex`;
- when it is omitted, HASHI derives Fixed for a session-capable active backend
  and Flex for a stateless active backend;
- an explicit Fixed default paired with a stateless active backend fails
  configuration validation;
- a valid persisted `agent_mode` takes precedence over `default_mode`;
- `default_mode` remains the fallback for missing, retired, or unsupported
  persisted mode values.

## Commands and transitions

- `/mode` shows only Fixed and Flex.
- `/mode fixed` enables native-session behavior after the capability check.
- `/mode flex` disables native-session behavior and persists Flex.
- `/backend` is a Flex-only action. From Fixed it presents an explicit
  switch-to-Flex confirmation before opening backend selection.
- `/mode memory+` is a compatibility alias that enables Memory+ without
  changing Fixed or Flex. `/memory plus on|off` is the canonical control.

## Retired-mode migration

Wrapper, Audit, and Dual-brain are historical working modes, not selectable
product choices.

- Persisted `wrapper`, `audit`, or `dual-brain` values migrate to the configured
  default, adjusted to Flex if the active backend is stateless.
- Historical `core`, `wrapper`, `wrapper_slots`, `audit`, and `dual_brain`
  configuration blocks are preserved. Migration changes only the working-mode
  owner; it does not erase rollback or historical data.
- `/mode wrapper`, `/mode audit`, `/mode dual-brain`, their former slash
  controls, and old inline callbacks return one compatibility notice and do not
  mutate state.
- Retired controls are hidden from `/help` and the Telegram command picker.

The old implementation records remain available as historical references:

- [Wrapper Agent Mode Development Plan](WRAPPER_AGENT_MODE_PLAN.md)
- [Audit Agent Mode Design Plan](AUDIT_AGENT_MODE_PLAN.md)
- [Dual-Brain Structure Design and Implementation Plan](DUAL_BRAIN_STRUCTURE_PLAN.md)

## Regression contract

Release tests must prove:

1. the public mode constants, keyboard, help metadata, and callback surface
   expose only Fixed and Flex;
2. session-capable backends default to Fixed and stateless backends default to
   Flex;
3. typed and callback transitions persist the selected mode and update backend
   session behavior;
4. Fixed selection on a stateless backend is rejected without mutation;
5. retired persisted modes migrate without deleting historical blocks; and
6. retired commands and stale callbacks remain harmless compatibility notices.
