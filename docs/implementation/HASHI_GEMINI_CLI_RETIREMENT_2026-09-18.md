# Gemini CLI progressive retirement

Status: implementation candidate for Issue #32. This record is not proof of
merge or live adoption.

## Decision

Gemini CLI is retired as a selectable or executable HASHI Engine. Retirement
is progressive because existing instance configuration still references the
Engine ID:

| Instance | allowed-backend references | active selections |
|---|---:|---:|
| H1 | 15 | 0 |
| H2 | 8 | 0 |
| H3 | 0 | 0 |
| H4 | 20 | 0 |

The audit read only agents.json backend declarations. It did not inspect
secrets, start an adapter, or modify an instance.

## Current contract

- Existing gemini-cli rows remain parseable and visible to migration tools.
- New Agent creation, onboarding, local connection, backend pickers, model
  pickers, Wrapper/Audit choices, and gateway catalogues omit Gemini CLI.
- Any attempted startup or adapter construction returns an explicit retired
  error. A retired active Engine is skipped; HASHI does not choose a fallback.
- The direct adapter, packaging entry, and migration catalogue stay present
  while any instance reference remains.
- Antigravity is a separate supported Engine. Its registry entry, models,
  preflight, adapter, and launch mode are unchanged.
- HER v2 routing, budgets, compression, shell handling, and retry behavior are
  outside this change.
- Protected Core is outside this change.

## Removal gate

Delete the Gemini adapter, skill, dependency, and compatibility catalogue only
after all of the following are recorded:

1. Every managed instance has zero gemini-cli configuration references.
2. No published sample, onboarding path, or frontend advertises the Engine.
3. Migration and rollback evidence is retained independently of the adapter.
4. Linux UTC and Windows contract suites pass from the same committed
   candidate.

## Focused evidence

Before the implementation, the retirement contract produced seven expected
test failures: gateway exposure, adapter execution, startup fallback, picker
visibility, local connection, first-run discovery, and Agent creation side
effects. The focused contract passes after the change. Wider regression and
live adoption remain separate qualification steps.
