# NVivo 14 Qualitative Coding Windows EXP

Status: candidate

This EXP captures a Windows-only NVivo 14 workflow for Barry's local qualitative
coding tasks. It combines:

- Barry's provided NVivo 14 guide
- direct desktop validation on HASHI Windows
- explicit step order for setup, coding, queries, and exports
- failure memory about what blocks reliable desktop execution

## Intent

Use NVivo 14 locally, without Lumivero login, to:

- create or open a local project
- import and organise qualitative sources
- code text into nodes
- attach memo links and maintain an audit trail
- run core queries, especially Matrix Coding Query
- generate reports, framework outputs, and exports
- practise safely inside the built-in sample project before touching live data

## Current learned shape

The current validated route is:

1. launch `NVivo.exe` locally
2. ignore `Log In` for local-only work
3. use `New Project` or the built-in sample project from the start screen
4. if the sample project tour appears, close it before trying to navigate
5. expect the main in-project UI to be less accessible to raw UI Automation than
   start-screen dialogs
6. prefer deliberate, reversible actions and local practice data
7. record every meaningful lesson in playbooks, validators, and failure memory

## Production rule

For this EXP, "done" means:

1. the intended NVivo project opens in desktop NVivo
2. local-only setup is respected unless Barry explicitly requests cloud/login
3. core task outputs exist, such as nodes, memos, queries, matrices, or exports
4. the result is validated through actual NVivo desktop state, not assumed from
   theory alone
5. any automation blind spots or manual-check requirements are stated clearly

## What this EXP knows so far

- `New Project` can be opened and its wizard is richly exposed to desktop
  automation on this machine.
- The start screen buttons are visible, enabled, focusable, and invokable.
- The first sample project button opens a local project window titled
  `Sample Project (2).nvp`.
- The sample project may open with an NVivo tour overlay that must be dismissed.
- After the project opens, many panes are only weakly exposed to raw UI
  Automation, so screenshot/OCR-style computer use is safer than relying only on
  control names.
- Keyboard navigation is more reliable than control discovery for some areas:
  `CTRL+1` Files, `CTRL+2` Codes, `CTRL+3` Cases, `CTRL+4` Memos, `CTRL+6`
  Maps, `CTRL+7` Formatted Reports, `F1` Help.
- Current UI terminology may differ from the guide. The guide says `Nodes`, but
  the current project UI on this machine exposes `Codes` in automation-visible
  labels.
- Window screenshots captured from the live NVivo process are a practical way to
  validate the true desktop state when richer GUI tools are unavailable.
- In the sample project, opening `Overview of Sample Project` can trigger a
  modal `Waiting for printer connection...` dialog that blocks progress until
  cancelled.
- The printer popup can be fixed at the Windows level by disabling automatic
  default printer management and setting `Microsoft Print to PDF` as the default
  printer.
- Treat NVivo `Save Reminder` dialogs as important safety prompts. For normal
  project work, save progress when the reminder appears because legacy desktop
  software can crash under large files or long sessions.
- After any unexpected NVivo close, hang, or crash, explicitly confirm whether
  the process is still running, whether the project reopens, and whether recent
  coding/memo work is still present before continuing.
- `Quick Coding` actions are unsafe to automate blindly. In editable mode,
  attempted code-name entry can land in the document body or heading rather than
  the intended quick-coding field.
- `Quick Coding` is workable when the preconditions are correct: the source
  must have a real active text selection, the bottom `Code to` pane must be
  enabled, and focus must be verified inside the `Code to` pane before pasting
  the code name.
- Creating a code from the `Codes` panel is stable through `Create -> Code`.
  It opens a `New Code` dialog with the `Name` field focused.
- Short keyboard selections can be created by clicking into read-only source
  text and sending `SHIFT+RIGHT` repeatedly. This is safer than broad `CTRL+A`
  when only a small passage is needed.

## Scope limit

Do not treat this as a generic NVivo skill yet. It still needs more runs across:

- real interview imports
- full coding inside the detail view
- matrix query generation with saved outputs
- framework matrix authoring
- report export validation
- project backup settings and close-without-save paths
