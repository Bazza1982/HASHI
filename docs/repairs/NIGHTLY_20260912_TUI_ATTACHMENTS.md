# Nightly repair: TUI attachment admission

## Scope

This repair connects local file selection, clipboard images, drag/drop path
input, and target Workzone `@file` references to the existing Workbench media
admission path. It changes Functions, TUI, and Remote code only; protected Core
is unchanged.

## Contract

- `/attach <path>` freezes the bytes at selection time. Later filesystem
  changes cannot alter the submitted payload.
- `/attach clipboard` reads a PNG without mutating the clipboard.
- a dropped or pasted file path is equivalent to `/attach <path>`;
  `@relative/path` is resolved only under the selected Agent's active target
  Workzone.
- one caption and one attachment are admitted in one Workbench request and one
  media Run. Admission failure never degrades into a text-only send.
- Remote transports actual bytes under authenticated `tui_proxy_v1`; it never
  transports the source machine path. Size, base64 syntax, and SHA-256 are
  checked before the peer Workbench call.
- pending data is bound to client connection generation, instance, and Agent.
  Switching any of them clears it, and a late response cannot render in the new
  scope.
- Workzone references are protocol-relative rather than native Windows paths,
  so their captions use the same quote-removal rules on every launch platform.
  Native `/attach` paths retain the platform path parser.

The TUI limit is 25 MiB per attachment. Directories, traversal, ambiguous
byte/reference requests, corrupt digests, and unavailable Workzones fail
closed.

## Focused validation

The focused attachment, session API, Workbench upload, Remote proxy, and TUI
scope suites cover frozen-byte behavior, one-request media admission, target
Workzone containment, Remote field forwarding, and corrupt/ambiguous payload
rejection. Native Windows clipboard and drag/drop adoption are validated
separately during the platform pass. The authenticated H1-to-H3 byte-transfer
acceptance is recorded in `NIGHTLY_20260912_RUNTIME_ACCEPTANCE.md`.
