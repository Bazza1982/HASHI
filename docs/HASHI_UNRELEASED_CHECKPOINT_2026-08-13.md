# HASHI Unreleased Development Checkpoint — 2026-08-13

> Status: integrated code checkpoint on `main`, documentation and publication
> preparation in progress. This is not a tagged release and is not evidence that
> a running HASHI or Aptenra process has loaded the checkpoint.

## Checkpoint identity

| Item | Value |
| --- | --- |
| HASHI code checkpoint | `ed5dcc9e` (`fix(her): bridge legacy screenshot results`) |
| Certified HER package | `0.1.0-hashi.10` |
| HER source commit | `85a481d9e5c94804ed9c0bd300ca9a635732c22d` |
| Certified Linux SHA-256 | `882c9a71013bdd6155558ff4dc8df4a8e002188e144b04f7fda2fb96f0f83ac2` |
| Release state | Unreleased; no GitHub push performed |
| Runtime state | Standalone HASHI adoption still requires an explicit reboot and live smoke |
| Aptenra state | Separate integration, Windows build, provenance, and product certification required |

The HER package identity is pinned in
`hashi_assets/her/manifest.json` and
`hashi_assets/her/certification_baseline.json`. The manifest's Windows entry is
still the older `0.1.3-hashi.3` binary built from `b27f4180`; it must not be
described as parity with the certified Linux `.10` package.

## Developments incorporated

### HER backend and debug runtime

- HER is the canonical public backend ID. `claw-cli` remains a migration alias.
- Fixed-mode HER uses the packaged runtime's persisted session ID and
  incremental turns. Flex and composed modes are intended to use HASHI-owned
  full-context turns, but the current HER adapter has no `set_session_mode()`
  hook and still resumes a non-ephemeral stored HER session. That mismatch is a
  publication gate because it can combine full-context injection with HER's
  previous internal context.
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

Two distinct, agent-local implementations currently coexist and must not be
presented as one store or one lifecycle:

| Path | Owner and storage | Activation and behavior | User surface |
| --- | --- | --- | --- |
| Runtime governance path | `orchestrator/her_habits.py`; SQLite under `backend_state/her/` | Intake-gated Planning and post-run Meditation produce candidates/evidence before promotion | `/skill habits` |
| Adapter-direct path | `adapters/her_habits.py`; JSON under `habits/` plus durable job/audit records | Default-off configuration; validated `create`/`update`/`delete` actions become active immediately | HER-only `/habit` |

The adapter-direct path now includes `/habit` status, view, on/off/default,
recoverable delete/delete-all/reset, full-detail audit, bounded retry/recovery,
and one proactive notification when Verbose was enabled and Meditation made a
real change.

Both paths are agent-local and HER-gated, but their policy and persistence
semantics differ. If the adapter-direct path is enabled while runtime governance
eligibility is also true, both paths can retrieve Habits and schedule Meditation
for the same foreground run. Mutual exclusion or an explicit consolidation
decision is therefore a release gate before enabling adapter-direct Habit
Meditation by default.

### Other recent HASHI developments represented by this checkpoint

- Workbench and Kasumi added versioned template binding, safe XLSM import, and
  preservation of Nexcel worksheet dimensions.
- Aptenra voice work added continuous conversation wake behavior, ambient-noise
  adaptation, model-native persona/delivery settings, stricter read-only
  translator fallbacks, and progress/caption ownership fixes.
- Embedded HASHI routing and debug runtime asset resolution were aligned with
  the canonical HER backend and current runtime package names.

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
- Aptenra/HASHI IP-boundary validation passed after explicit MIT classification
  of this checkpoint document;
- the changed-file high-signal secret scan found zero findings;
- `git diff --check` passed.

## Release and rollout gates still open

1. Decide whether the two HER Habit paths will be mutually exclusive,
   consolidated, or intentionally run together with separately documented
   semantics.
2. Add and test an explicit HER session-mode policy so Flex/composed full-context
   turns do not also resume a prior HER session, while fixed mode retains
   incremental resume.
3. Run `/reboot min` on a canary HER agent, then verify session continuity,
   `/habit`, media intake, `media_read`, screenshot results, notification
   behavior, and failure recovery.
4. Only after the canary is green, run the intended wider `/reboot max` rollout
   and capture live evidence.
5. Build and certify a current Windows HER package before claiming Linux/Windows
   parity.
6. Keep Aptenra adoption separate until its embedded runtime, Windows artifact,
   provenance, and full product suite are explicitly updated.

## GitHub publication readiness

This repository is a mixed-license tree: HASHI's inherited open-source scope and
the proprietary Aptenra product scope coexist. Before any GitHub push:

1. confirm the destination repository URL, owner, branch, and visibility;
2. confirm that the selected publication scope is compatible with `LICENSE`,
   `LICENSE_SCOPE.md`, `REUSE.toml`, and all third-party notices;
3. inspect staged files and ensure workspace state, credentials, logs, private
   media caches, generated binaries outside the reviewed package, and local
   operator material are excluded;
4. run the release gates in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md);
5. create an intentional commit, review the final diff and commit range, then
   push only after the GitHub remote and publication boundary are approved.

At this checkpoint the local repository has no `origin` or upstream tracking
branch. The existing remote is a LAN debug remote, not an approved GitHub
publication target. Documentation can be committed locally, but a GitHub push
must wait for an explicit destination and visibility decision.
