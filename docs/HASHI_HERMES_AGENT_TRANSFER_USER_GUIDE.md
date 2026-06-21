# HASHI Hermes Agent Transfer User Guide

## Scope

This guide covers the implemented HASHI Hermes transfer package workflow.

Supported directions:

- HASHI agent -> `.hashi-hermes-agent` -> Hermes profile.
- Hermes profile -> `.hashi-hermes-agent` -> HASHI agent.

Safety defaults:

- Imports create disabled or review-mode targets.
- Schedules import as paused review drafts.
- Secrets, delivery credentials, active sessions, and runtime caches are not
  migrated.
- Move finalization refuses to disable the source unless the operator provides
  `--target-verified`.

## Commands

All commands run from the HASHI repository root:

```bash
python3 scripts/hermes_transfer.py <command> [args]
```

Every command emits JSON. A failed command returns exit code `2` and prints a
JSON error to stderr.

## HASHI to Hermes

### Plan export

```bash
python3 scripts/hermes_transfer.py plan-hashi-export \
  --hashi-root /home/lily/projects/hashi \
  --agent zelda \
  --output /tmp/zelda.hashi-hermes-agent
```

Review:

- `dry_run_plan.planned_writes`
- warnings
- package path

### Export package

```bash
python3 scripts/hermes_transfer.py export-hashi \
  --hashi-root /home/lily/projects/hashi \
  --agent zelda \
  --output /tmp/zelda.hashi-hermes-agent
```

The package contains normalized identity, memory notes, disabled schedule
drafts, source evidence, audit reports, and checksums.

### Plan Hermes import

```bash
python3 scripts/hermes_transfer.py plan-hermes-import \
  --profile-dir ~/.hermes/profiles/zelda \
  --bridge-home "$HASHI_CONNECT_HERMES_HOME" \
  --package /tmp/zelda.hashi-hermes-agent
```

Review:

- target profile name
- `AGENT.md` write
- `config.yaml` review-mode merge
- `agents.yaml` disabled bridge entry
- memory and schedule import folders

### Import into Hermes

```bash
python3 scripts/hermes_transfer.py import-hermes \
  --profile-dir ~/.hermes/profiles/zelda \
  --bridge-home "$HASHI_CONNECT_HERMES_HOME" \
  --package /tmp/zelda.hashi-hermes-agent
```

After import:

- Read `~/.hermes/profiles/zelda/hashi_import/audit/migration_report.md`.
- Read `~/.hermes/profiles/zelda/hashi_import/audit/post_migration_self_check.md`.
- Confirm `agents.yaml` has `enabled: false` and `review_required: true`.
- Restart or reload Hermes only after review.

### Finalize HASHI source move

Only for move mode, after the Hermes target is manually verified:

```bash
python3 scripts/hermes_transfer.py finalize-move-source \
  --direction hashi-to-hermes \
  --target-verified \
  --hashi-root /home/lily/projects/hashi \
  --agent zelda \
  --package-id <package_id>
```

This disables the HASHI source agent and that agent's scheduler jobs.

## Hermes to HASHI

### Plan export

```bash
python3 scripts/hermes_transfer.py plan-hermes-export \
  --profile-dir ~/.hermes/profiles/xiaoye \
  --bridge-home "$HASHI_CONNECT_HERMES_HOME" \
  --agent xiaoye \
  --output /tmp/xiaoye.hashi-hermes-agent
```

Review:

- exported profile identity
- accepted and skipped memory files
- excluded runtime state warnings
- paused cron drafts
- excluded credential warnings

### Export package

```bash
python3 scripts/hermes_transfer.py export-hermes \
  --profile-dir ~/.hermes/profiles/xiaoye \
  --bridge-home "$HASHI_CONNECT_HERMES_HOME" \
  --agent xiaoye \
  --output /tmp/xiaoye.hashi-hermes-agent
```

### Plan HASHI import

```bash
python3 scripts/hermes_transfer.py plan-hashi-import \
  --hashi-root /home/lily/projects/hashi \
  --package /tmp/xiaoye.hashi-hermes-agent
```

The default target HASHI agent is disabled and marked `import_review_required`.

### Import into HASHI

```bash
python3 scripts/hermes_transfer.py import-hashi \
  --hashi-root /home/lily/projects/hashi \
  --package /tmp/xiaoye.hashi-hermes-agent
```

After import:

- Review `workspaces/<agent>/hermes_import/audit/migration_report.md`.
- Review `workspaces/<agent>/hermes_import/audit/post_migration_self_check.md`.
- Confirm `agents.json` has `is_active: false`.
- Confirm imported tasks are disabled review drafts.

### Finalize Hermes source move

Only for move mode, after the HASHI target is manually verified:

```bash
python3 scripts/hermes_transfer.py finalize-move-source \
  --direction hermes-to-hashi \
  --target-verified \
  --profile-dir ~/.hermes/profiles/xiaoye \
  --bridge-home "$HASHI_CONNECT_HERMES_HOME" \
  --profile xiaoye \
  --package-id <package_id>
```

This disables the Hermes bridge entry, writes disabled transfer metadata into
the Hermes profile `config.yaml`, and creates
`DISABLED_BY_HASHI_TRANSFER.md`.

## Rollback

Each import and move-finalization step creates or updates rollback evidence:

- HASHI imports:
  `private/hermes_transfer/rollback/<package_id>-<timestamp>/`
- Hermes imports:
  `$HASHI_CONNECT_HERMES_HOME/private/hermes_transfer/rollback/<package_id>-<profile>-<timestamp>/`

Rollback is intentionally manual in this phase. Restore the captured files
after checking the rollback manifest.

## Verification Checklist

Before enabling any imported agent/profile:

- Package reads successfully with checksum verification.
- Migration report matches expected source and target.
- No secrets or delivery credentials were imported.
- Sessions and runtime caches are absent.
- Imported schedules are disabled.
- Target identity file is readable.
- Target agent/profile responds to a self-check in its native runtime.
- For move mode, source is disabled only after the target self-check succeeds.
