# Fixed and Flex Working Modes

Status: **accepted product contract for HASHI v4.0.0-alpha.2 (updated 2026-09-07)**.

HASHI exposes two Agent working modes: **Fixed** and **Flex**. Fixed is the
default when the selected backend can preserve a native session. Flex assembles
bridge-managed context for each request. Backend selection is available in both
modes.

## Keep the terms separate

| Layer | Current choices | What it controls |
|---|---|---|
| Configured Agent runtime | `type: "flex"` for normal Agents | One workspace, identity, backend manager, and command surface |
| Agent working mode | Fixed or Flex | Native-session continuation versus bridge-managed context |
| HER execution mode | Direct, Strategic, or Planned | How much HER orchestration one task receives |
| Memory+ | On or Off | Optional compact continuity, independent of working mode |

Fixed does not restore the retired legacy `type: "fixed"` runtime. It is a
session-preserving working mode inside `FlexibleAgentRuntime`.

## Runtime contract

| Behavior | Fixed | Flex |
|---|---|---|
| Default | Yes, for a session-capable active backend | Yes, when the active backend is stateless |
| Context | Reuses the backend's native session and sends incremental turns once resume is available | HASHI assembles the applicable context for each request |
| Backend switching | `/backend` is available directly | `/backend` is available directly |
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
- after the current mode-policy migration, a valid persisted `agent_mode`
  takes precedence over `default_mode`;
- `default_mode` remains the fallback for missing, retired, or unsupported
  persisted mode values.

### One-time Fixed-default migration

State written before Fixed became the session-capable product default has no
`agent_mode_policy_version`. On its first load under policy version 1, a legacy
persisted `flex` value is migrated to `fixed` only when the configured default
and active backend support Fixed. HASHI then writes
`agent_mode_policy_version: 1`.

The marker makes this migration one-shot. A later explicit `/mode flex` writes
Flex together with the current policy version, so subsequent reloads preserve
the user's choice. Stateless backends and an explicit `default_mode: "flex"`
are never forced into Fixed.

## Commands and transitions

- `/mode` shows only Fixed and Flex.
- `/mode fixed` enables native-session behavior after the capability check.
- `/mode flex` disables native-session behavior and persists Flex.
- `/backend` opens backend selection directly in Fixed and Flex, with no
  intermediate switch-to-Flex confirmation. Typed selections work in both modes.
- A successful backend selection atomically saves the backend/model and working
  mode: Fixed when the target adapter reports session support, otherwise Flex.
  This also applies when reselecting the current backend and when the previous
  mode was explicitly Flex. `/mode flex` remains available after selection.
- Target initialization and fresh-session setup finish before committing the
  selection. If preparation or persistence fails, the original live backend,
  native session, working mode, and saved selection remain intact.
- Failed model-button selection keeps model choices available for retry. Busy
  Agents keep the existing menu and receive an alert; this decision uses runtime
  state rather than matching an English word in a translated failure message.
- Memory+ is independent and remains unchanged. Old confirmation buttons only
  reopen backend selection; they no longer change the mode.
- Plain and `+` (continuity) selection retain their handoff semantics; continuity
  selection does not determine the working mode.
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
5. retired persisted modes migrate without deleting historical blocks;
6. retired commands and stale callbacks remain harmless compatibility notices;
7. Fixed and Flex both open backend selection directly, including typed and
   callback selections;
8. successful selection persists Fixed for session-capable targets and Flex for
   stateless targets, preserving Memory+ on and off;
9. initialization, session setup, or state-write failures preserve the original
   backend object, native session, mode, and saved state.
