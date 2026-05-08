# NVivo 14 Qualitative Coding Playbook

Use this playbook for Barry's Windows-local NVivo 14 qualitative coding tasks.

## Operating mode

- Work locally first. Do not log in unless Barry explicitly wants cloud features.
- Save projects on a local SSD, not a network drive.
- Before long NVivo sessions, make sure Windows is not waiting on a slow default
  printer. Disable automatic default printer management and use
  `Microsoft Print to PDF` as the default printer when printer popups appear.
- Save frequently. When NVivo shows a `Save Reminder`, treat it as a real
  project-safety checkpoint and save unless the current session is explicitly a
  disposable/no-save experiment.
- Watch for unexpected crashes or silent exits. If NVivo disappears, hangs, or
  restarts, stop and verify project integrity before doing more coding.
- Prefer the built-in sample project for learning, dry runs, and UI rehearsal.
- Treat memo links and coding stripes as part of the audit trail, not optional
  decoration.

## A. Local project setup

1. Launch `NVivo 14`.
2. Stay on local mode:
   - do not create a Lumivero ID
   - do not log in for local-only work
3. Create a project:
   - start screen -> `New Project`
   - or `FILE -> New Project`
4. In the New Project wizard:
   - set `Project title`
   - review or edit `File name`
   - choose a local save path with `Browse...`
   - optionally add a short `Description`
   - keep `Text content language` aligned with the dominant source language
   - click `Next`
5. After project creation, enable backups:
   - `File -> Info -> Project Properties -> Backup`
   - enable daily backups

## B. Safe learning project

Use the official sample project before touching real data.

1. Close any open project.
2. Start screen -> `Sample Project`
3. Choose the Multi-method sample route available on this machine.
4. If the tour appears:
   - click `SKIP TOUR`
   - or close the tour before trying to inspect the main interface
5. Confirm the project window title changed away from the blank start screen.

## C. Interface orientation

Once inside a project, orient to these areas:

- ribbon tabs: `Home | Insert | Analyze | Query | Explore | View | Layout`
- left pane: navigation view
- middle pane: list view
- right pane: detail view
- bottom: status bar

Target areas to verify:

- `Files -> Internals`
- `Nodes` or `Codes` depending on current UI terminology
- `Cases`
- `Classifications`
- `Queries`
- `View -> Coding Stripes -> All Nodes`

Keyboard navigation shortcuts validated or documented for Windows use:

- `CTRL+1` -> Files
- `CTRL+2` -> Codes/Nodes
- `CTRL+3` -> Cases
- `CTRL+4` -> Memos
- `CTRL+5` -> Queries area, but the visible label may not say `Queries`
- `CTRL+6` -> Maps
- `CTRL+7` -> Formatted Reports
- `F1` -> NVivo Help
- `F5` -> refresh workspace
- `CTRL+SHIFT+E` -> export selected item
- `CTRL+E` -> toggle edit/read-only mode for files and maps
- `CTRL+Q` -> move between codable content and the Quick Coding bar

## D. Import and organise data

For Barry's own projects:

1. `Files -> Internals`
2. right-click -> `Import -> Files` or `Import -> Folders`
3. create subfolders for collection discipline:
   - `Interviews`
   - `Focus Groups`
   - `Field Notes`
   - `Documents`
4. create classifications and attributes when cross-case comparison matters:
   - `Classifications -> New Classification`
   - add attributes such as `Age`, `Gender`, `Location`
5. create `Cases` for participants when case-based querying will matter later

Avoid:

- importing live research data before a dry run with a disposable project
- storing active projects on a slow network location
- mixing unrelated source types into one flat Internals folder

## E. Coding workflow

Coding is the core skill.

1. Open a source from `Files -> Internals`
2. Read in detail view
3. Highlight target text
4. Code with one of these methods:
   - drag the selection to the `Nodes` pane
   - `Home -> Code` and choose an existing node
   - right-click -> `Code In Vivo`
   - Quick Coding bar after verifying the active selection and input focus
5. Create new nodes when needed:
   - in `Nodes`, right-click -> `New Node`
   - use clear names and descriptions
   - current NVivo 14 path on HASHI: `Create -> Code`, then fill the `New Code`
     dialog
6. Turn on coding visibility:
   - `View -> Coding Stripes -> All Nodes`
   - if stripes do not appear, check that the file is not in edit mode
7. Add reflexive documentation:
   - right-click source or node -> `New Memo Link`
   - write why the passage matters, not just what it says
8. Test reversibility:
   - right-click coded text -> `Uncode`

Best practice:

- code a transcript twice when useful:
  - inductive pass
  - deductive pass
- use parent and child nodes deliberately
- aggregate coding from children when parent themes need roll-up reporting

Avoid:

- creating many vague nodes with overlapping meaning and no memo trail
- coding without visible stripes or later verification
- treating In Vivo labels as final themes without later cleanup
- trying to use coding stripes while the file is still in edit mode
- typing into `Quick Coding` or `Code to` fields unless the input focus has been
  visually confirmed
- assuming `CTRL+Q` alone is enough. It may work only after a real source text
  selection exists and the bottom `Code to` pane is enabled
- leaving the sample project open in an edited state after failed automation;
  discard changes and reopen cleanly

### Quick Coding bar procedure validated on HASHI

1. Open a source in detail view.
2. Keep the source in `Read-Only` mode unless content editing is required.
3. Create a real active text selection:
   - keyboard path: click the document body and use `CTRL+A` for a broad
     disposable training selection
   - mouse path: drag-select text and screenshot-check that selection is black
     with inverted text
4. Confirm the bottom quick-coding controls are enabled:
   - `In` should show `Codes`
   - `Code to` should no longer be disabled
5. Click inside the bottom `Code to` input area.
6. Verify focus, when possible, by checking the focused element bounds are
   inside the bottom `Code to` field, around the horizontal bar at the bottom of
   the source window.
7. Paste the code name with clipboard paste, then press `Enter`.
8. Validate success through:
   - source row `Codes` count increments
   - source row `References` count increments
   - status bar shows `Codes: 1 References: 1`
   - `Codes` panel contains the newly created code

### Code creation from the Codes panel

Validated stable path:

1. Open the `Codes` area in the left navigation.
2. Go to ribbon `Create`.
3. Click `Code`.
4. In the `New Code` dialog, type the code name in `Name`.
5. Press `Enter` or click `OK`.
6. Confirm the new code appears in the `Codes` list with `Files 0` and
   `References 0` before it is used.

Validated example:

- `Round4 Panel Code` appeared in the Codes list after `Create -> Code`.

### Short selection

Validated stable path:

1. Keep the document in `Read-Only`.
2. Click into the source text near the intended passage.
3. Use `SHIFT+RIGHT` repeated enough times to select a short phrase.
4. Screenshot-check the selected passage appears as black background with
   inverted text.

Do not assume mouse drag-selection will work in this embedded document surface.
Keyboard selection was more reliable during training.

## F. Queries and analytic power

Windows NVivo's strongest payoff is query work.

### Word Frequency

1. `Analyze -> Query -> Word Frequency Query`
2. set scope, usually project-wide for exploration
3. run
4. inspect the list and word cloud

Use for:

- early familiarisation
- checking vocabulary dominance

Important limits:

- stop words are excluded by default
- scanned PDFs that are image-only need OCR before import if word-frequency
  analysis is expected to work
- framework matrix summaries are not searched by word-frequency queries

### Text Search

1. `Analyze -> Query -> Text Search Query`
2. search a term such as `development` or `change`
3. enable stemming when appropriate
4. run

Use for:

- tracing language patterns
- spotting candidate passages before coding

### Matrix Coding Query

This is the highest-value query for qual comparison.

1. `Analyze -> Query -> Matrix Coding Query`
2. drag nodes/themes into `Rows`
3. drag cases or attributes into `Columns`
4. click `Run`
5. double-click any cell to jump to underlying coded text
6. if the preview matters, save the results into `Results` or `Coding Matrices`
   so the matrix becomes part of the project record

Use for:

- comparing themes across participant groups
- checking differences by location, role, or demographic attribute

Avoid:

- running matrices before cases and attributes are clean
- trusting matrix counts without drilling into the actual coded text
- assuming a temporary preview is preserved unless it is explicitly stored

## G. Visualisations and reports

Use `Explore` for outputs that summarise the analysis.

1. `Explore -> Charts`
2. `Explore -> Mind Map`
3. `Explore -> Framework Matrix`
4. `Explore -> Reports`

Priority outputs:

- `Node Report`
- `Coding Summary Report`
- `Framework Matrix Report`
- `Attribute Table`

Export path:

- right-click the result -> `Export`
- choose `Word`, `Excel`, or `PDF` as needed

Avoid:

- exporting before checking the underlying node, case, or matrix logic
- assuming visualisations are analytically meaningful without memo context

## H. Guided practice sequence

Follow this order when training or onboarding:

1. open sample project
2. dismiss tour
3. inspect `Files`, `Nodes`, `Cases`, `Classifications`
4. turn on coding stripes
5. create one disposable node
6. code one short passage
7. attach one memo
8. run Word Frequency
9. run Text Search
10. run Matrix Coding Query
11. create one chart or framework output
12. export one report
13. close project carefully

## I. Desktop automation notes

- Start-screen buttons and the New Project wizard are automation-friendly here.
- In-project panes are not richly named through raw UI Automation on this
  machine.
- Keyboard shortcuts can reveal state changes even when pane names are sparse.
- Some documentation and UI labels drift between `Nodes` and `Codes`, and query
  commands may appear under different tabs depending on release/help version.
- Capture a live window screenshot before risky input when GUI/OCR tooling is
  limited. This is the most reliable way to verify where focus and modals really
  are.
- If a `Save Reminder` appears during real project work, save and then continue.
  Only choose not to save during deliberately disposable sample-project training.
- After suspected crash or forced close:
  - check whether `NVivo.exe` is still running
  - reopen the project from the expected local path
  - confirm the latest source, codes, references, memos, and query outputs still
    exist
  - record any lost work or recovery step in the run notes
- For Quick Coding, treat enabled-state as a gate. If the bottom `Code to` pane
  is disabled, there is no active text selection and typing is unsafe.
- The bottom `Code to` field behaves like a token field. It may append pasted
  text to an existing token instead of replacing it, even when focus is inside
  the field. Do not use it to switch from one existing code to another unless the
  old token has been visibly cleared.
- Treat modal environment popups as blockers. The sample project can raise a
  `Waiting for printer connection...` dialog while opening the overview file.
- If that printer popup repeats, set:
  - `HKCU\Software\Microsoft\Windows NT\CurrentVersion\Windows\LegacyDefaultPrinterMode = 1`
  - default printer = `Microsoft Print to PDF`
- If a task requires reliable deep interaction in the detail view, prefer:
  - screenshot/OCR-capable computer use tools
  - slow human-like desktop control
  - explicit checkpointing after each action

Do not assume control-name discovery alone will be enough once a project is
open.
