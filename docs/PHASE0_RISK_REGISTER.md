# Phase 0 Risk Register

## Highest Risks

### R-001: YAML round-trip damages workflow meaning

- Why it matters: valid YAML can still represent different execution semantics
- Likely failure modes:
  - dropped unknown fields
  - changed dependency semantics
  - destructive normalization of legacy files
- Mitigation:
  - `docs/ROUND_TRIP_CONTRACT.md`
  - fixture corpus with unknown-field and legacy cases
  - blocking fidelity warnings in GUI

### R-002: GUI layout misleads users about execution order

- Why it matters: users will infer left-to-right or top-to-bottom order from the canvas
- Likely failure modes:
  - node movement interpreted as ordering change
  - disconnected edges hidden by auto-layout
- Mitigation:
  - make `depends` the only source of execution truth
  - visually emphasize dependency edges
  - require export warnings when mapping is ambiguous

### R-003: Event model is underspecified too late

- Why it matters: API, CLI, engine, and GUI can drift into incompatible status models
- Likely failure modes:
  - missing correlation ids
  - GUI polling state that cannot be reproduced from engine events
  - hard-to-debug failures after extraction
- Mitigation:
  - `docs/LOGGING.md`
  - Phase 1 builds logging before extraction

### R-004: Feature parity is claimed while hidden HASHI coupling remains

- Why it matters: extraction can compile but still lose real behavior
- Likely failure modes:
  - notifier behavior disappears
  - step handling loses timeout/retry/cancel semantics
  - evaluator/debug semantics become implicit and untestable
- Mitigation:
  - parity checklist
  - fixture-based tests
  - protocol design that carries structured results

### R-005: Legacy workflow dialects get ignored

- Why it matters: the repo already contains workflow-like YAML that does not match the current schema
- Likely failure modes:
  - parser rejects files users still rely on
  - GUI "imports" them by silently reshaping data
- Mitigation:
  - explicit Class C compatibility path
  - preserve-and-warn policy instead of destructive migration

## Watch Risks

### R-006: Comments and ordering are treated as cosmetic

- Mitigation: keep them explicit in the contract and unknown-fields fixture

### R-007: Runtime state and editor draft get conflated

- Mitigation: immutable runtime snapshot shape in `docs/LOGGING.md`
