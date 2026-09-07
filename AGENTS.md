# Working on HASHI

Before editing, read `ARCHITECTURE.md` and `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`.
For command/UI changes also read `docs/HASHI_COMMAND_UI_STYLE_GUIDE.md`;
choose verification using `docs/TESTING_POLICY.md`.

- State the change's functional owner (PCM, PAO, HER v2, or Frontend Connector),
  engineering layer, and focused validation before editing. One short update is enough.
- Normal features belong in Functions or platform/instance configuration.
  Put behavior in the narrowest existing owner; derive views instead of copying
  model/effort lists, command metadata, default ports, or state writers.
- Protected paths are owned solely by `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
  Run `python scripts/check_protected_core_changes.py` when choosing files and
  before finishing. Its default covers staged, unstaged, and untracked changes.
- Core edits require a concrete reason and explicit task authorization. Existing
  authorization remains valid for its stated scope; do not ask for it again.
  Naming a feature or approving an unrelated change is not blanket Core approval.
  `--authorized` records an authorization already given; it does not grant one.
  Do not persist `HASHI_CORE_EDIT_AUTHORIZED=1` in configuration or shell profiles.
- Model/effort opt-ins for an instance belong in `allowed_backends` and resolve
  through `runtime_effort_options`; do not edit the protected shared compatibility
  baseline merely to add an instance model. Shared catalogue/Engine compatibility
  changes are planned Core work, not a reason to remove a protected path.
- UI wording belongs in renderers and runtime language catalogs. Use shared card
  and navigation helpers, escaped HTML values, and the user's chosen UI locale.
  The retired Workbench compatibility identifiers mean Backend API; they do not
  name an active HASHI frontend.
- Test real observable behavior and persistence/failure boundaries. For a bug,
  record how the focused test fails before the fix and passes afterward.
  A fake switching function or copied expected prose does not verify actual switching.
- Respect other work in the checkout. Keep local identities, secrets and machine
  paths in ignored configuration. Never infer identity from the folder name.
- Reboot/restart is an operational action, not a test shortcut. Follow the current
  user's scope. If forbidden, finish code and offline checks and explicitly leave
  live adoption unverified. Source changes, qualified artifacts, and running Worker
  generations are separate facts; report which was checked.
- Update the owning decision and FYI when behavior changes. Record approval,
  implementation and live verification separately, scoped to branch/instance.

Install the lightweight local hook once with
`python scripts/install_engineering_hooks.py`. It preserves existing custom hooks.
The hook checks the staged Core diff and whitespace; CI remains the branch check.
