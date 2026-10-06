# Gemini CLI retirement

Decision: remove the old Gemini CLI Engine and delegation skill. Antigravity
CLI (`antigravity-cli`, executable `agy`) is the replacement. Authorized on
2026-10-06; implementation and adoption are recorded separately for HASHI3.

Functional owner: PAO. Engineering layer: Functions, with instance
configuration retaining its existing authority and revision rules. Core is
unchanged.

The observed Google error rejected an unsupported **client**. It was not
evidence that the entire Google account or every Gemini model was unavailable.
Retirement makes repairing or retesting that old client unnecessary.

## Executable boundary

The shared catalogue, adapter factory, startup/Gateway preflights and live
permission normalization reject `gemini-cli`. The old adapter and skill are
removed from source and release packaging. Native-browser, wrapper and audit
choices no longer offer the client. The `antigravity` skill uses the existing
agy transport and its own current model identifiers.

Old `gemini-cli` configuration fails with an actionable replacement message
before migration publication or process startup. It is deliberately not an
alias: agy model names, native sessions and credentials differ. Choose an
actual agy model and an explicit authorized Antigravity row before publishing
the configuration through its existing owner. An already retired persisted
selection that is no longer allowed follows the existing configured-backend
restoration policy; it cannot create an old adapter.

Historical engine IDs, messages, failure records and usage receipts remain
unchanged and render under their original client name. Gemini models in
Antigravity and Google/OpenRouter Model Provider rows remain available under
their existing qualification and permission rules. No machine-global CLI
uninstallation, account logout or credential deletion is part of this change.

## Validation

The retirement regression checks the real Backend API catalogue, factory,
configuration migration and preflight boundaries. On the pre-fix source,
eight rejection checks failed and the historical identity check passed; the
same nine checks pass after the implementation. Three first-run regressions
also failed before their fix and now pass, including rejection before creating
any configuration or workspace files. Direct consumers and the
registry/configuration gate must pass before adoption. Package inventory must
exclude the old adapter/skill and include the replacement skill.

HASHI3 implementation does not establish deployment on another instance.
Actual Function adoption, agy execution and interactive hardware/browser
acceptance are separate facts in the scoped execution record.
