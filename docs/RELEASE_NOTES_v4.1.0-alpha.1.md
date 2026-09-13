# HASHI v4.1.0-alpha.1 — Functional Generation Alpha

Release classification: **Functions generation change with protected Core
unchanged**. The accepted versioning policy therefore advances `Y` from the
published `4.0.0` target to `4.1.0` and resets the maintenance number. This is
the first alpha candidate for that target: application label
`4.1.0-alpha.1`, Python package identity `4.1.0a1`.

This local candidate is not a publication record. A source commit, qualified
Functions generation, running instance, tag and distributed artifact are
separate facts. No existing tag or artifact is overwritten, and publication
still requires the release checklist and explicit user approval.

## Functional scope

- Agent Move schema 4 adds explicit **Agent only** and **Move home** modes,
  PCM-owned continuity export, a decimal 1 GB uncompressed home limit, and
  authenticated file-backed AES-GCM streaming without whole-package Base64.
- One Move/Clone confirmation is durably adopted by a shared PAO background
  manager. It survives the source Worker stopping, verifies real target Worker
  and connector readiness, performs owner-specific source cleanup, retries
  recoverable interruption, and keeps `continue`/`cancel` as admin recovery.
- Exact Model Provider capability facts are discovered asynchronously, cached
  with provenance and revision, intersected with Adapter transport and instance
  policy, and consumed consistently by runtime and HER v2 media admission.
- Current-message source provenance and signed private-resource authorization
  are carried across PAO/PCM/Frontend boundaries without granting authority
  from model-generated text.

## Included maintenance and usability work

- HER v2 preserves malformed native Tool-call evidence and performs bounded,
  explicit repair rather than losing the Provider failure reason.
- Provider-response and stop-decision diagnostics, `/version` provenance,
  Move/Clone separation, TUI usability, and command metadata remain part of the
  reviewed local integration scope.

## Compatibility and rollout

- Agent Move archive schemas 1–3 remain readable for historical recovery. New
  remote transactions require schema 4 transfer-mode and authenticated-binary
  capabilities and fail before target/source mutation when the peer is old.
- Imported schedules remain disabled drafts. Clone never copies the source
  Telegram credential. Move keeps source-first single-consumer cutover.
- Windows and WSL use the same product source while retaining independent
  instance configuration, credentials, paths and running-generation adoption.
- Final validation records the exact source/Core/Functions identities and live
  adoption separately for each authorized instance.

## Alpha boundary

The alpha label permits focused operational validation; it does not certify all
optional Engines, external services, enterprise deployment profiles or
platform combinations. Known external/user-dependent canaries remain explicit
rather than being inferred from offline tests.
