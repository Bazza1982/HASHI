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
| Linux x86-64 | `0.1.0-hashi.20` | `5ed5b30ef2ab0f80ab6d4fd08a1b7b64e77faf05` | Current certified HASHI1 package |
| Windows x86-64 | `0.1.0-hashi.20` | `5ed5b30ef2ab0f80ab6d4fd08a1b7b64e77faf05` | Native build and local-command smoke certified |

The certified Linux `.20` SHA-256 digest is
`3c601931478d645c17c9317a6975dcba0944ff48731d2991d70b3af4ffa59167`.
The native Windows `.20` SHA-256 digest is
`5463a3d006edcb61a6d066d9b1441046602b03fbb37e207988315a073d8ef3b6`.
Its embedded Git SHA is the same pinned source commit, and native `version`,
`doctor`, `status`, and `prompt --help` stdin-capability smokes passed.

## Verification

```bash
python scripts/her_runtime_probe.py --check version
python scripts/verify_her_certification.py --source-root /path/to/her-source
```

The certification command checks the pinned source identity, full Rust
workspace tests, and workspace/all-target Clippy with warnings denied. Forty
pre-existing upstream diagnostics are pinned by exact package, path, line and
lint; any addition, removal or movement fails certification. Runtime adoption
still requires a canary reboot and live provider/tool/media smoke.

See [the active backend contract](../../docs/HER_BACKEND_CONTRACT.md) and the
[2026-08-13 unreleased checkpoint](../../docs/HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md).
