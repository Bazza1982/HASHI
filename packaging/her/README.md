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
| Linux x86-64 | `0.1.0-hashi.22` | `246b04e9fa28ef0b6f74c2d924ab3697b95197bd` | Current certified HASHI1 package |
| Windows x86-64 | `0.1.0-hashi.22` | `246b04e9fa28ef0b6f74c2d924ab3697b95197bd` | Native build and local-command smoke certified |

The selected revision is certified by tag
`her-0.1.0-hashi.22-certified-r2`. The certified Linux `.22` SHA-256 digest is
`e6c88b9dd37c9191f9aad0df9fd0cf9bbeb4365778a10153a48b4cf752096c91`.
The native Windows `.22` SHA-256 digest is
`cd127b283d0bb8aa5db9d1863a617bb84a2c8cd0174ed305c72c5f97b294724d`.
Its embedded Git SHA is the same pinned source commit, and native `version`,
`doctor`, `status`, and `prompt --help` stdin-capability smokes passed.

The rejected Linux `.21` artifact remains in the versioned release directory for
forensic provenance only. It was built from a source line that omitted the certified
HASHI execution/session contract, so the manifest must not select it.

## Verification

```bash
python scripts/her_runtime_probe.py --check version
python scripts/verify_her_certification.py --source-root /path/to/her-source
```

The certification command checks the certified tag and complete-history source
bundle, pinned source identity, both packaged binary hashes and embedded provenance,
full Rust and HASHI integration suites, and workspace/all-target Clippy with warnings
denied. Forty inherited diagnostics are pinned by exact package, path, line and lint;
any addition, removal or movement fails certification. Runtime adoption still requires
a Momo canary reboot and live provider/tool/media/continuity smoke.

See [the active backend contract](../../docs/HER_BACKEND_CONTRACT.md) and the
[2026-08-13 unreleased checkpoint](../../docs/HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md).
