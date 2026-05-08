# NVivo Desktop Validators

Use these checks before treating an NVivo qualitative coding run as successful.

## Launch and local mode

- `NVivo.exe` launches and shows a visible main window.
- Local-only work does not require login when Barry did not request cloud use.
- The project title changes from the blank start screen to the intended project.
- The default printer is not an offline or slow network printer when repeated
  printer-connection popups have been observed.
- If a `Save Reminder` appeared during real work, the project was saved before
  continuing.
- If NVivo unexpectedly closed or hung, the process state and reopened project
  contents were checked before any further coding.

## Setup

- New project wizard opens when requested.
- Project path is local, not a network location.
- Backup settings are reviewed or enabled when creating a real project.

## Sample project familiarisation

- Sample project opens from the start screen.
- Any tour overlay is dismissed before navigation validation starts.
- Core areas are reachable: `Files`, `Nodes`, `Cases`, `Classifications`,
  `Queries`.
- If labels differ, the EXP records the live UI term, for example `Codes`
  instead of `Nodes`.
- At least one keyboard navigation path is validated when control-name discovery
  is weak.

## Coding

- A source opens in detail view.
- Any blocking modal dialog, such as printer-connection prompts, is dismissed
  before coding attempts begin.
- A node can be created or selected.
- A newly created code appears in the `Codes` panel with expected `Files` and
  `References` counts.
- A passage can be coded and, if needed, uncoded.
- Quick Coding success can be validated by incremented `Codes` and `References`
  counts on the source row and by the new code appearing in the `Codes` panel.
- Coding Stripes can be enabled for verification.
- At least one memo link can be created when reflexive notes are part of the
  task.
- Input focus is visually confirmed before typing into quick-coding controls.
- After saving or recovering from a crash, representative codes/references are
  checked again to confirm work was not lost.

## Queries

- Word Frequency can run on the intended scope.
- Text Search can run with the intended term and options.
- Matrix Coding Query can run with defined rows and columns.
- At least one matrix cell can be drilled into when matrix interpretation
  matters.
- If query results are meant to persist, they are saved explicitly rather than
  left as a temporary preview.

## Outputs

- Reports or matrices export to the requested format.
- Any exported file exists at the recorded path.
- Remaining manual verification needs are stated explicitly.
