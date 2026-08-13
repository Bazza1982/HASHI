# HASHI Unreleased Development Checkpoint — 2026-08-13

> Status: reviewed publication candidate for `main`. This is not a tagged
> release and is not evidence that a running HASHI process has loaded the
> checkpoint.

## Checkpoint identity

| Item | Value |
| --- | --- |
| HASHI code checkpoint | `2270f5be` (`fix(her): enforce session and habit ownership`) |
| Certified HER package | `0.1.0-hashi.10` |
| HER source commit | `85a481d9e5c94804ed9c0bd300ca9a635732c22d` |
| Certified Linux SHA-256 | `882c9a71013bdd6155558ff4dc8df4a8e002188e144b04f7fda2fb96f0f83ac2` |
| Release state | Unreleased source checkpoint; public `main` publication candidate |
| Runtime state | Standalone HASHI adoption still requires an explicit reboot and live smoke |

The HER package identity is pinned in
`hashi_assets/her/manifest.json` and
`hashi_assets/her/certification_baseline.json`. The manifest's Windows entry is
still the older `0.1.3-hashi.3` binary built from `b27f4180`; it must not be
described as parity with the certified Linux `.10` package.

## Developments incorporated

### HER backend and debug runtime

- HER is the canonical public backend ID. `claw-cli` remains a migration alias.
- Fixed-mode HER uses the packaged runtime's persisted session ID and
  incremental turns. Flex and composed modes use HASHI-owned full-context
  turns; `set_session_mode()` clears stale checkpoints, prevents `--resume`,
  and ignores replacement session IDs for those turns.
- Provider selection, provider/model discovery, structured streaming,
  provider-visible reasoning fragments, incomplete-run recovery, and effort
  budgets through `max+` are integrated.
- The HASHI Tool Gateway exposes the allowed `ToolRegistry` capabilities to HER
  over a private MCP stdio bridge, preserving existing permission and audit
  enforcement.
- The certified `.10` runtime treats MCP `isError` results as failures and
  preserves bounded MCP image results as model-visible multimodal input for
  Anthropic and OpenAI-compatible providers.
- The HER debug lab and Superloop template provide scripted provider/MCP
  fixtures, restart guards, evidence records, and Flash-before-Pro iteration.

### HER multimedia and multimodal work

- Telegram image and document inputs are passed to HER as actual media rather
  than filename-only hints.
- `media_read` gives HER a bounded, audited path for local images, PDFs, audio,
  and video. Text extraction remains the preferred PDF path; rendered pages are
  a fallback when visual inspection is required.
- Gateway normalization now accepts canonical MCP image content and legacy
  screenshot-style results while validating MIME type, size, count, ordering,
  and audit metadata.
- Historical sessions retain only private cache references and safely degrade
  unavailable media; raw base64 payloads are not persisted in normal session
  output.

The implementation and remaining live rollout matrix are recorded in
[HER Multimedia and Multimodal Plan](her_multimedia_multimodal_plan.md).

### HER Habit and Meditation work

The HER adapter is the authoritative agent-local Planning/Meditation owner.
Standalone HASHI has one JSON store under `habits/`, with durable job and audit
records under `backend_state/`. Validated `create`, `update`, and `delete`
actions become active immediately and are managed through HER-only `/habit`.

The adapter-direct path now includes `/habit` status, view, on/off/default,
recoverable delete/delete-all/reset, full-detail audit, bounded retry/recovery,
and one proactive notification when Verbose was enabled and Meditation made a
real change.

`HERAdapter.habit_pipeline_owner` makes this ownership explicit for downstream
compatibility consumers, while request-scoped eligibility keeps internal,
maintenance, non-HER, and ephemeral requests out of the Habit path. `/habit
off` therefore disables ordinary HER Habit learning completely.

The complete unreleased feature ledger remains the top-level
[`CHANGELOG.md`](../CHANGELOG.md); this checkpoint records the integration and
release boundary for the latest development wave rather than replacing it.

## Verification evidence

The development checkpoint recorded these green gates before this documentation
pass:

- HASHI Python suite: `3701 passed, 100 skipped, 0 failed`;
- focused HER multimedia follow-up: `81 passed, 2 skipped, 0 failed`;
- HER Rust workspace: `1468 passed, 1 ignored, 0 failed`;
- HER Clippy: workspace/all-targets clean with warnings denied;
- packaged HER metadata, source provenance, and SHA-256 certification: passed.

Documentation-only changes still require `git diff --check`, internal Markdown
link validation, a sensitive-file/status review, and focused tests for any
contract statement that cannot be established from the existing checkpoint.

This documentation pass completed those lightweight gates with:

- `165 passed` across release-readiness, IP-boundary, HER certification
  metadata, Tool Gateway, media, Habit, and runtime-pipeline tests;
- `152` internal Markdown links checked with no missing targets;
- the publication-boundary review classified this checkpoint as HASHI scope;
- the changed-file high-signal secret scan found zero findings;
- `git diff --check` passed.

The subsequent session/Habit gate closure ran a unified regression selection
covering HER, `/habit`, ownership guards, mode switching, reboot,
Gateway/media, screenshot audit, and UI locale policy: `369 passed, 2 skipped,
0 failed`. The two skips are existing environment-conditional cases, not
failures hidden by the ownership changes.

The clean public integration then ran a 24-file HER/Habit/session/media/Gateway/
Superloop selection: `428 passed, 0 failed`. That run also found and fixed the
Debug Lab's dependency on machine-local `ajiao` state; its focused clean-clone
regression is `15 passed` (`66ce0ffe`).

## Release and rollout gates still open

Session-mode ownership and Habit-pipeline ownership are fixed with automated
regression coverage in `2270f5be`. The remaining gates are operational and
platform-specific:

1. Run `/reboot min` on a canary HER agent, then verify session continuity,
   `/habit`, media intake, `media_read`, screenshot results, notification
   behavior, and failure recovery.
2. Only after the canary is green, run the intended wider `/reboot max` rollout
   and capture live evidence.
3. Build and certify a current Windows HER package before claiming Linux/Windows
   parity.

## GitHub publication readiness

Before publishing this checkpoint:

1. confirm the destination repository URL, owner, branch, and visibility;
2. confirm that the selected publication scope is compatible with `LICENSE`,
   packaged `CLAW_LICENSE`, and all third-party notices;
3. inspect staged files and ensure workspace state, credentials, logs, private
   media caches, generated binaries outside the reviewed package, and local
   operator material are excluded;
4. run the release gates in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md);
5. create intentional commits, review the final diff and commit range, then
   push without overwriting remote history.

The approved destination is the public `Bazza1982/HASHI` repository on its
`main` branch. Publication uses a clean HASHI checkout, with only reviewed
HASHI commits transferred and tested before the direct `main` push.
