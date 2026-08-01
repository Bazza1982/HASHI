# Contributing to HASHI

The canonical engineering rule is:

> Design for high cohesion, low coupling, a single source of truth, and
> localized change.

Before editing, read
[`docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`](docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
It defines the four runtime layers, current fact owners, hot-change contract,
and protected process core.

## Change rules

1. Put behavior in the narrowest existing owner. Do not copy model lists,
   command metadata, manager construction, workspace state writers, instance
   identity, ports, or process-lock paths.
2. Keep UI, behavior, persistence, bootstrap, platform adaptation, and local
   instance adoption separate.
3. Prefer a small direct module over a speculative framework. Add abstraction
   when it removes present duplication or protects a real boundary.
4. Treat a small change spanning many unrelated files as Shotgun Surgery.
   Establish one owner and derive compatibility views before adding the feature.
5. Feature code should adopt through `/reboot`. Process-bootstrap changes must
   say clearly that a cold restart is required.
6. Never infer instance identity from a directory name. Read local
   `agents.json` / `instances.json`.
7. Never use a computer-wide HASHI process lock or process-name kill scan.
   Resolve the configured instance's PID and lock paths through
   `orchestrator.pathing`.
8. Keep private machine paths out of tracked runtime and adapters. Local tools
   take paths from instance config, command arguments, or environment variables.

## Required checks

```bash
python scripts/check_protected_core_changes.py --validate-manifest
python scripts/check_protected_core_changes.py
python -m pytest -q
git diff --check
```

Protected core edits require explicit authorization and focused regression
tests. On GitHub, an authorized core pull request also needs the
`core-change-approved` label.
