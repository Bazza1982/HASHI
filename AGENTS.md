# Working on HASHI

Before editing, read `ARCHITECTURE.md` and `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`.
For command/UI changes also read `docs/HASHI_COMMAND_UI_STYLE_GUIDE.md`;
choose verification using `docs/TESTING_POLICY.md`.

- Before editing, state the functional owner (PCM, PAO, HER v2, or Frontend
  Connector), engineering layer, and focused validation.
- Normal features belong in Functions or platform/instance configuration.
  Put behavior in the narrowest existing owner; derive views instead of copying
  model/effort lists, command metadata, default ports, or state writers.
- Protected paths are owned solely by `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
  Run `python scripts/check_protected_core_changes.py` when choosing files and
  before finishing. Its default covers staged, unstaged, and untracked changes.
- Treat every protected Core path as immutable. Editing, moving, renaming, or
  deleting one is allowed only when the current user explicitly authorizes a
  **Core major-version migration**. A bug fix, refactor, reboot request, broad
  approval, or request to "protect Core" is not that authorization.
- An authorized Core migration must raise the product major version, reset its
  minor and patch numbers, carry the `core-change-approved` pull-request label,
  and add a matching independent review record under `docs/core-reviews/`.
  The implementer and reviewer must differ. Batch one Core generation into one
  reviewed pull request rather than splitting it across ordinary changes.
- `--authorized` and command-scoped `HASHI_CORE_EDIT_AUTHORIZED=1` only record
  authorization already given; neither bypasses the major-version and review
  gates. Never persist Core authorization variables in configuration or shell
  profiles. See `docs/HASHI_CORE_PROTECTION_HARDENING_2026-09-13.md`.
- Model/effort opt-ins for an instance belong in `allowed_backends` and resolve
  through `runtime_effort_options`; do not edit the shared catalogue merely to
  add an instance model. Shared catalogue/Engine compatibility belongs to the
  qualified Functions generation. Never move product behavior back into Core.
- Core imports no product module, including lazy imports. Shared services run
  in a replaceable Function process. `/reboot` retains its Agent scope; the
  separate shared replacement operation is broad and needs operational scope.
- UI wording belongs in renderers and runtime language catalogs. Use shared card
  and navigation helpers, escaped HTML values, and the user's chosen UI locale.
  The retired Workbench compatibility identifiers mean Backend API; they do not
  name an active HASHI frontend.
- Test real observable behavior and persistence/failure boundaries. For a bug,
  record how the focused test fails before the fix and passes afterward.
  A fake switching function or copied expected prose does not verify actual switching.
- Respect other work in the checkout. Keep local identities, secrets and machine
  paths in ignored configuration. Never infer identity from the folder name.
- For migrated HASHI JSON configuration, reuse `orchestrator.config_json` and
  retain the read revision; a display fallback is never a writable snapshot.
  Do not blindly retry a conflict or a committed durability error. See
  `docs/HASHI_CONFIGURATION_PERSISTENCE.md` for scope and classified exceptions.
- Reboot/restart is an operational action, not a test shortcut. Follow the current
  user's scope. If forbidden, finish code and offline checks and explicitly leave
  live adoption unverified. Source changes, qualified artifacts, and running Worker
  generations are separate facts; report which was checked.
- Update the owning decision and FYI when behavior changes. Record approval,
  implementation and live verification separately, scoped to branch/instance.

Install the local hook with `python scripts/install_engineering_hooks.py`; it
keeps custom hooks and checks staged Core changes/whitespace. CI remains final.
