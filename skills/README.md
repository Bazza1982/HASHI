Skills live in one folder per skill.

Required file:
- `skill.md`

Supported types:
- `action`
- `prompt`
- `toggle`

Built-in notes:
- `cron` and `heartbeat` are treated as bridge-managed built-ins.
- `recall` is a bridge-managed toggle skill for one-shot auto-restore after unexpected restart.
- Toggle state is persisted per agent workspace in `skill_state.json`.
