# NVivo 14 Guide Familiarisation 001

Date: 2026-05-09
Status: completed
EXP target: `barry/nvivo_qualitative_coding_windows`

## Goal

Convert Barry's NVivo 14 Windows guide into a reusable candidate EXP for local
qualitative coding, while validating as much as possible through the desktop app
on HASHI Windows.

## Training materials

- Barry-provided `NVivo 14 for Windows – Comprehensive Guide for Qualitative Researchers`
- official help URL referenced in the guide:
  `https://help-nv.qsrinternational.com/14/win/Content/welcome.htm`

## What was executed

1. Confirmed `NVivo 14` was installed locally and launchable.
2. Confirmed the start screen buttons were visible, enabled, focusable, and
   invokable through desktop automation.
3. Verified local-only use is viable without login by launching the app and
   opening the sample project directly.
4. Opened the sample project from the start screen.
5. Observed a tour overlay and dismissed it with `SKIP TOUR`.
6. Re-checked the project window title and working state.
7. Opened `New Project` and validated that the wizard surfaces fields such as
   `Project title`, `File name`, `Description`, `Browse...`, and
   `Text content language`.
8. Confirmed the `Close` button actually shuts the app and that the app can be
   relaunched cleanly.
9. Recorded automation limits and converted the guide into a stepwise playbook,
   validators, and failure memory.

## What was observed

- The sample project opened under the window title `Sample Project (2).nvp`.
- The sample project tour can block further navigation until dismissed.
- Start-screen controls are much easier to automate than the in-project
  workspace.
- The project workspace exposes very few named controls through raw UI
  Automation and often shows `Loading content...` in Chromium-backed panes.
- Deep project work should therefore be validated with richer computer-use tools
  or careful human-supervised desktop control.

## Output

- `manifest.json`
- `EXP.md`
- `playbooks/qualitative_coding.exp.md`
- `validators/nvivo_desktop_validators.md`
- `failures/failure_memory.jsonl`
- `training_runs/nvivo14_guide_familiarisation_001/state/observations.md`
