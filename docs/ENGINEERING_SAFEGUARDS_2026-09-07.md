# Engineering safeguards — 2026-09-07

Owner: PAO and Frontend Connectors. Placement: Functions, configuration and local
development tooling, with a bounded Core correction for presentation delegation
and authoritative port defaults.

## Decision and authorization

The user approved the HASHI2 corrections and light protections on 2026-09-07.
This includes the identified Core placement/default corrections. Reboot/restart
is prohibited for this work. This record does not authorize other Core changes.

- Core source protection has one manifest; default checks include index and
  working-tree changes. Baseline/index manifests retain protection when a
  candidate edits the manifest itself. CI labels remain the existing PR approval
  mechanism. Local flags acknowledge an existing authorization, not grant one.
- A small root AGENTS.md routes engineering work to existing authoritative docs.
  A local pre-commit hook runs staged Core and whitespace checks without starting
  tests, providers or HASHI. Existing custom hooks must be preserved.
- Startup display labels belong to the banner renderer; compatibility service
  IDs remain unchanged. Backend API is the visible product term.
- Port fallbacks derive from runtime_defaults. Instance endpoints remain local.
- Fixed/Flex backend selection follows FIXED_FLEX_WORKING_MODES.md. UI notices
  share HTML/locale rules and cannot describe the retired confirmation flow.
- FYI is bounded current guidance, includes engineering rules and a content
  revision, and distinguishes actual runtime values from current source.

## Status and evidence

- Design: approved for HASHI2.
- Implementation: HASHI2 working tree; not a claim of publication or adoption.
- Regression evidence: the staged-only Core edit and staged-manifest scenarios
  failed with the old checker, then passed with the revised checker. Tests use
  temporary repositories. Backend selection tests invoke the real manager and
  verify success, rollback, Memory+, command/callback and continuity paths.
- The Chinese busy callback and successful-switch notice failed their focused
  tests before correction, then passed. Error routing now uses the success
  result rather than English words in the displayed message.
- The previous FYI contained 44,551 characters and was cut at the 12,000-character
  injection budget. The revised reference fits in full, and the combined primer
  is checked against a 16,000-character budget. The previous builder also ignored
  changes to root engineering guidance; the content revision now includes it.
- Offline validation on HASHI2: focused component selection, 381 passed; curated
  Core gate (`python -m pytest -q`), 565 passed; final UI/backend consumer selection,
  258 passed; all 23 modules in the architecture CI job, 326 passed. These scopes
  overlap and must not be added together. Compilation, Ruff and diff checks passed.
- The local staged-check hook is installed in this checkout. It starts no test
  suite or HASHI process. Existing architecture CI now includes the safeguards,
  UI, real backend-selection and FYI checks.
- Live adoption: not performed; reboot/restart was explicitly prohibited. Core
  corrections require a planned cold adoption; ordinary Function changes use
  targeted Worker adoption after a compatible Core is running.

## Retained boundary

The shared backend registry stays protected. Instance model/effort opt-ins already
have a Function/configuration route, which ordinary tasks must use. Full removal
of catalogue data from Core is a separate migration of long-lived gateway and
configuration consumers, not part of these limited corrections. Do not weaken the
Core manifest as a shortcut or advertise that such a migration has happened.

Read-only GitHub verification found that the shared repository's `main` has no
branch protection and no applicable branch ruleset. The existing check is named
`boundaries`. This HASHI2 change does not alter shared GitHub repository policy.
When promoting it, require that existing check; there is no need for another CI
job or an additional manual review gate. Local hooks can be bypassed, so they
reduce accidental mistakes but are not a substitute for required remote checks.
