# Packaged Claw Runtime

This directory is reserved for HASHI-owned packaged `hashi-claw` runtime assets.

Contract:

- `hashi_assets/claw/manifest.json` is the runtime source of truth.
- Binary paths in the manifest must be relative to `hashi_assets/claw`.
- Every packaged binary must declare a pinned `sha256`.
- Packaged discovery fails closed on checksum mismatch.
- Operators may choose `prefer-packaged`, `require-packaged`, or `system-only`.
- Large generated binaries live under `hashi_assets/claw/bin/` and are ignored by git.
- Until release binaries are built and added to `hashi_assets/claw/bin/`, `scripts/claw_code_probe.py` is expected to report `ClawBinaryNotFound` unless a system `claw` fallback is configured.
