# KVM Visual Session (Channel A)

## Goal

Observe user-visible state and perform **short** HID interactions. This channel
does **not** replace PiKVM product Web UI; operators may use the Web UI, and
agents may use the same control APIs/helpers.

## Capabilities

- HDMI snapshot / continuous view
- Absolute mouse (when online)
- Short key sequences: Escape, Enter, Win+D, Alt+F4, arrow keys
- Optional short Run-dialog commands (prefer Remote for anything longer)

## Lab defaults

- Management address: `10.0.0.3` (direct factory/management link)
- Credentials: project-controlled store only (never embed secrets in EXP)

## Safe procedures

1. Take a baseline snapshot and label it.
2. Prefer keyboard shortcuts over long typed paths.
3. Double-click desktop icons with carefully chosen coordinates; verify with a
   second snapshot.
4. Dismiss error dialogs with Enter/Escape after capturing the dialog text.
5. After UI actions, wait and re-snapshot before concluding success.

## Hard limits (from live failures)

- **Long HID text inject is unreliable** — characters drop, reorder, or mangle
  paths. Do not type full PowerShell one-liners through PiKVM print/HID.
- **No physical power button** on laptop via PiKVM — escalate to human.
- **No video** — stop UI claims; switch to remote-only diagnostics.
- Do not hold destructive multi-click storms; one intentional action, then verify.

## Evidence

- Keep snapshot paths or hashes in the evidence pack.
- OCR/dialog text is useful; never capture password fields into permanent logs.
