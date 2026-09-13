## Scope

- Functional owner:
- Engineering layer:
- Focused validation:

## Protected Core

- [ ] `python scripts/check_protected_core_changes.py --base <base-ref>` reports no protected Core change.

If protected Core is intentionally changed, replace the statement above with
the evidence below. A broad feature/fix approval is not Core authorization.

- [ ] The current user explicitly authorized a Core major-version migration.
- [ ] The product major version is higher than the base; minor and patch are zero.
- [ ] The pull request has the `core-change-approved` label.
- [ ] A new matching JSON review record exists under `docs/core-reviews/`.
- [ ] The independent reviewer is not the implementer.

## Adoption

- Source implementation:
- Offline verification:
- Live adoption (separate evidence, if authorized):
