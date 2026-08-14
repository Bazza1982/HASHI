# Packaged HASHI Engine Runtime (HER)

This directory documents HASHI-owned packaging for the built-in **HASHI Engine
Runtime (HER)**. HER is derived from the MIT-licensed Claw runtime; the original
Claw copyright and license notice remain distributed as `CLAW_LICENSE`.

Contract:

- `hashi_assets/her/manifest.json` is the runtime source of truth.
- Binary paths in the manifest must stay relative to `hashi_assets/her`.
- Every packaged binary must declare a pinned SHA-256 digest.
- Packaged discovery fails closed on checksum mismatch.
- Operators may choose `prefer-packaged`, `require-packaged`, or `system-only`.
- Versioned binaries live under `hashi_assets/her/releases/`.
- `scripts/her_runtime_probe.py` verifies HER runtime discovery and diagnostics.
- `scripts/verify_her_certification.py` verifies the pinned upstream Claw source
  against HER's certification baseline.

Public HASHI configuration uses the backend ID `her`. The legacy `claw-cli` ID
is accepted only as a migration alias.

## Current manifest entries

| Platform | Runtime | Source | Status |
| --- | --- | --- | --- |
| Linux x86-64 | `0.1.0-hashi.16` | `cc28e447f1f32f5a4325bbe1670e2568ed01749a` | Current certified standalone HASHI package |
| Windows x86-64 | `0.1.0-hashi.16` | `cc28e447f1f32f5a4325bbe1670e2568ed01749a` | Native Windows version smoke certified |

The certified `.16` SHA-256 digests are
`2af160e894f2387a7e54df4bcd226f44af98dd8b539e3d9e5702f01c68dca640`
for Linux and
`02934539ce8fa38213633c31130eaa1df70c647efdb2df138f3a0797a60da6ab`
for Windows. Both binaries use the same clean source lock; each platform keeps
its own checksum and runtime smoke evidence.

## Verification

```bash
python scripts/her_runtime_probe.py --check version
python scripts/verify_her_certification.py --source-root /path/to/her-source
```

The certification command checks the pinned source identity, full Rust
workspace tests, and workspace/all-target Clippy with warnings denied. Runtime
adoption still requires a canary reboot and live provider/tool/media smoke.

See [the active backend contract](../../docs/HER_BACKEND_CONTRACT.md) and the
[2026-08-13 unreleased checkpoint](../../docs/HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md).
