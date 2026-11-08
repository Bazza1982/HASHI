# CI execution policy

Updated 2026-11-09. Optimize confidence per minute, retaining security and failure checks.

- Ordinary commits run the owning component and its direct consumers.
- Docs/metadata alone do not run product, browser or native suites.
- Major/global dependency or interpreter changes, unbounded impact, shared test
  infrastructure and stable cross-domain release candidates retain full checks.
- A PR-ready event or small routine release alone is not a full-suite requirement.
- Use the workflow's manual full input (or an existing ci-full PR label where
  applicable) for an explicitly requested full candidate.
- Preserve native/browser qualification for their actual platform boundaries.
- Feature branches use PR checks; default-branch pushes still verify direct
  updates. Cancel obsolete runs for the same workflow and PR/ref.
- Unknown product impact expands; missing baselines and selected failed,
  cancelled, timed-out or missing results fail. They are never green skips.
- Preserve required Core/authority, privacy and source-publication guards.
- Reuse earlier results only when code/combined tree, dependencies, tests and
  environment still match; matching head SHA alone is insufficient proof.
- Local and hosted execution are separate choices from scope. A local result
  never fabricates a provider-bound required status.
- No schedules, paid upgrades, new approval counts, visibility changes or
  runtime/service deployment are introduced by this policy.
