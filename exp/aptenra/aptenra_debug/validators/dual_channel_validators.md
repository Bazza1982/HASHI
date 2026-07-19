# Dual-Channel Validators

Use before marking a dual-channel debug or onsite support task complete.

## Adjacency and preflight

- [ ] Network adjacency stated: `lan` or `vpn` (or failure `adjacency_missing`).
- [ ] Channel plan recorded before destructive actions.
- [ ] At most one channel owns destructive operations at a time.

## Visual channel (when claimed)

- [ ] At least one snapshot or human-described frame matches the conclusion.
- [ ] No residual unexplained blocking dialog if UI success is claimed.
- [ ] Long HID text injection was not used for multi-line scripts.

## Remote channel (when claimed)

- [ ] Authenticated shell reached the intended host/user (or documented failure).
- [ ] Log/process/git probes used for proof rather than assumption alone.
- [ ] Scripts run from files when commands are non-trivial.
- [ ] No secrets printed into chat, EXP, or evidence JSON.

## Start Aptenra (when that is the task)

- [ ] Shortcut or launcher entry verified.
- [ ] Remote shows ready signal **or** explicit failure with log evidence.
- [ ] KVM shows companion/shell **or** documented `remote_only` limitation.
- [ ] Second-start hang / ACL dialog patterns checked against failure memory.

## Git sync (when that is the task)

- [ ] Operator and target HEADs match the intended commit after sync.
- [ ] Dirty target tree was stashed or consciously discarded with note.
- [ ] Launcher copy refreshed if entrypoint scripts changed.
- [ ] MEGA/OneDrive were not used as source-of-truth transport.

## Evidence

- [ ] Evidence JSON exists with `at`, `ok`, channels, and topic.
- [ ] No PIN, password, API key, or full secret material in the pack.

## Product-tier neutrality

- [ ] Outcome does not assume Personal-only semantics unless the task is Personal.
- [ ] Same playbooks apply to other single-device tiers under adjacency.
