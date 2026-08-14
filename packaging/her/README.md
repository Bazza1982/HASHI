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
| Linux x86-64 | `0.1.0-hashi.19` | `79be4613e37d03781713253a04aa64aedf3f1902` | Current certified HASHI1 package |
| Windows x86-64 | `0.1.0-hashi.19` | `79be4613e37d03781713253a04aa64aedf3f1902` | Native build and local-command smoke certified |

The certified Linux `.19` SHA-256 digest is
`3cd9dbee8617b7fb23a7df7893cc2a3bd17a70b0d0c3fa5945f41ab88f674538`.
The native Windows `.19` SHA-256 digest is
`f483723f249e89b08eec2f091553e1dc2e207dbe9565a819a41c264b9e3f00f5`.
Its embedded Git SHA is the same pinned source commit, and native `version`,
`doctor`, `status`, and `prompt --help` stdin-capability smokes passed.

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
