# Nagare Known Limitations

This file is the release-facing list of current Nagare v1 limitations.

## Engine

- `resume` is still a placeholder CLI path, not a completed lifecycle feature.
- The extracted core still defaults its run storage to `flow/runs`, which is practical for this monorepo but not yet a fully separated package default.
- Some engine modules still reflect HASHI-era terminology in comments and user-facing strings. This is cosmetic debt, not a protocol dependency.

## YAML and editor fidelity

- Phase 4 guarantees exact no-op round trips and safe `x-nagare-viz` updates. It does not yet guarantee arbitrary structural edits for every unsupported workflow shape.
- Compatibility class `B` and `C` documents must fall back to raw YAML for unsupported changes.
- Comment and ordering preservation are guaranteed only on the supported export paths described in [`docs/ROUND_TRIP_CONTRACT.md`](/home/lily/projects/hashi/docs/ROUND_TRIP_CONTRACT.md).

## Runtime observation

- The API is read-only.
- The GUI runtime overlay is polling-based, not streaming-based.
- The GUI correlation ID is currently front-end local and not yet bound to a live engine session identifier.

## Packaging

- `nagare-viz` is built from source in this repo; it is not published as a separate npm package.
- The monorepo currently exposes `nagare` through the root Python package metadata rather than a fully split `nagare-core/` repository layout.
