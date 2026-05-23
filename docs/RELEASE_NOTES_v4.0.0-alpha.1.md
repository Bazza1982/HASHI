# HASHI v4.0.0-alpha.1 — Release Notes

Release focus: **Claw mode foundation + Superloop operational foundation**.

This is an alpha release. It marks a major architectural direction change from
the v3.2 line, but it does not claim stable unattended automation or complete
Claw Tool Gateway parity.

## Why This Is v4 Alpha

v3.2.1 was a hotfix release for Workbench, HChat, and Remote route recovery.
v4.0.0-alpha.1 introduces new execution and orchestration foundations:

- HASHI can treat Claw/OpenClaw as a scoped backend through `claw-cli`.
- HASHI can resolve Claw providers through its own config and secrets chain.
- HASHI can discover a packaged Claw runtime through manifest metadata and
  checksum validation.
- HASHI now has an explicit Superloop function contract for long-running
  controller loops.

These changes affect runtime boundaries, release packaging, permission policy,
agentic file work, and long-running orchestration semantics. That is major
version territory, but alpha because key parity and packaging work remains.

## Claw Mode Foundation

Added:

- `adapters/claw_cli.py`
- backend registry entry for `claw-cli`
- provider-aware Claw configuration
- Claw binary resolution with explicit/global/packaged/env/PATH ordering
- packaged runtime manifest placeholder:
  `hashi_assets/claw/manifest.json`
- packaging docs under `packaging/claw/`
- provider smoke probe:
  `scripts/claw_code_probe.py`

Validated:

- focused Claw adapter tests pass;
- provider smoke paths for OpenRouter and DeepSeek were live-validated;
- local Ollama/OpenAI-compatible behavior was validated against a mock server;
- momo live integration validated repo-root read/write/edit through Claw mode.

## Superloop Operational Foundation

Added:

- `docs/SUPERLOOP_FUNCTION_CONTRACT.md`
- release checklist gates for Superloop schema, waits, HChat reply handling,
  inbox drain, and template/live-loop validation

The contract defines:

- required loop files;
- `state.json` minimum fields;
- `taskboard.json` `task_id` schema;
- `waits.json` resume policy;
- issue register behavior;
- HChat reply classification;
- closeout barriers;
- validation gates.

## Known Alpha Limits

- Packaged `hashi-claw` binaries are not yet shipped under
  `hashi_assets/claw/bin/`.
- `scripts/claw_code_probe.py --check version` may correctly return
  `ClawBinaryNotFound` until release binaries are present or a system `claw`
  fallback is configured.
- Claw Tool Gateway/MCP parity is planned, not complete.
- Claw browser/web parity and shell/test execution require later release gates.
- Superloop is not yet a stable unattended automation product; controller loops
  must keep explicit evidence, waits, issues, and closeout records.

## Verification Snapshot

Focused checks used during release preparation:

```text
python -m py_compile setup.py adapters/claw_cli.py tests/test_claw_cli_adapter.py
python -m pytest tests/test_claw_cli_adapter.py -q
git diff --check
```

Live momo evidence:

```text
backend: claw-cli
model: deepseek/deepseek-v4-flash
repo-root read: passed
repo-root write: passed
repo-root edit: passed
```
