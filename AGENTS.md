# Working on HASHI

Before editing, read `ARCHITECTURE.md`, `docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`,
and `docs/TESTING_POLICY.md`. For command/UI work also read
`docs/HASHI_COMMAND_UI_STYLE_GUIDE.md`.

- Before editing, state the functional owner (PCM, PAO, HER v2, or Frontend
  Connector), engineering layer, and focused validation.
- Normal features belong in Functions or platform/instance configuration.
  Use the narrowest owner; derive views instead of copying model/effort lists,
  command metadata, ports, or state writers.
- Protected paths come only from `orchestrator.runtime_contract.CORE_SOURCE_PATHS`.
  Run `python scripts/check_protected_core_changes.py` when choosing files and
  before finishing; it covers staged, unstaged, and untracked changes.
- Treat every protected Core path as immutable. Editing, moving, renaming, or
  deleting one is allowed only when the current user explicitly authorizes a
  **Core major-version migration**. A bug fix, refactor, reboot request, broad
  approval, or request to "protect Core" is not that authorization.
- An authorized Core migration raises the product major version, resets minor
  and patch, carries `core-change-approved`, and adds a matching independent
  review under `docs/core-reviews/`. Implementer and reviewer must differ. Put
  one Core generation in one reviewed pull request.
- `--authorized` and command-scoped `HASHI_CORE_EDIT_AUTHORIZED=1` only record
  authorization already given; neither bypasses version/review gates. Never
  persist Core authorization variables. See
  `docs/HASHI_CORE_PROTECTION_HARDENING_2026-09-13.md`.
- Model/effort opt-ins for an instance belong in `allowed_backends` and resolve
  through `runtime_effort_options`; do not edit the shared catalogue merely to
  add an instance model. Shared catalogue/Engine compatibility belongs to the
  qualified Functions generation. Never move product behavior back into Core.
- Core imports no product module, including lazily. Shared services run in a
  replaceable Function process. `/reboot` stays Agent-scoped; separate shared
  replacement needs broad operational scope.
- Never install or upgrade a Function dependency in a running instance's Core
  interpreter. Optional/native Function dependencies run in an isolated
  sidecar selected by platform or instance configuration. A normal Function
  change must finish through hot `/reboot`; never propose a Core cold restart
  as its adoption or recovery path.
- UI wording belongs in renderers/catalogs. Use shared card/navigation helpers,
  escaped HTML, and the chosen UI locale. Retired Workbench compatibility names
  mean Backend API, not an active HASHI frontend.
- Test observable behavior and persistence/failure boundaries. For a bug, record
  focused red/green evidence. Fake switching or copied prose proves no switch.
- Respect other checkout work. Keep identities, secrets, and machine paths in
  ignored configuration. Never infer identity from a folder name.
- For migrated HASHI JSON configuration, reuse `orchestrator.config_json` and
  retain the read revision; a display fallback is never a writable snapshot.
  Do not blindly retry a conflict or a committed durability error. See
  `docs/HASHI_CONFIGURATION_PERSISTENCE.md` for scope and classified exceptions.
- Reboot/restart is an operational action, not a test shortcut. Follow the current
  user's scope. If forbidden, finish code and offline checks and explicitly leave
  live adoption unverified. Source changes, qualified artifacts, and running Worker
  generations are separate facts; report which was checked.
- When the current user asks to "run essential live test", use
  `docs/HASHI_FRONTEND_LIVE_ACCEPTANCE.md` and the versioned suite manifest. This
  means real interaction through the named external frontend, not only automated
  tests. Resolve the exact checkout, instance, and Agent before acting; do not infer
  a broader target or future authorization. Keep screenshots, runtime receipts,
  source/runtime snapshots, and operator observations distinct, and require every
  mandatory item plus the final Core/source invariant to pass.
- Update the owning decision and FYI when behavior changes. Record approval,
  implementation and live verification separately, scoped to branch/instance.

Install the local hook with `python scripts/install_engineering_hooks.py`; it
preserves custom hooks and checks staged Core/whitespace. CI remains final.
