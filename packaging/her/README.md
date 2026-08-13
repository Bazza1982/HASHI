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
| Linux x86-64 | `0.1.0-hashi.12` | `7ce6cf431502ca5a78a1874f09db9c2de7211562` | Current certified standalone HASHI package |
| Windows x86-64 | `0.1.3-hashi.3` | `b27f4180802a7735f745ddd764e5b9b1b88f7e9f` | Older reviewed artifact; not `.12` parity |

The certified Linux `.12` SHA-256 is
`7e14a22bb51d9c99de3eb92c627434adf631fed898fc4dca420a8faf2b6a9a32`.
Do not infer Windows parity from the shared manifest. A current Windows build
needs its own reproducible source lock, checksum, product integration, and test
evidence.

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
