# HASHI3 nightly repair and delivery — 2026-10-08

The current user requests six named nightly items, confined to HASHI3. HASHI4
code/runtime are excluded. Existing desktop/Workbench changes and all of the
user's open applications must be preserved. Test source, deployed bytes, actual
client observations and physical-device/hearing feedback are distinct facts.

Functional ownership: HERV3 owns commentary in Functions; PAO owns Session
concurrency and Remote discovery; Frontend Connector owns the terminal speech
path and external client transport/presentation. Client packaging and installed
feature choices remain platform/instance configuration. No Core migration is
authorized or implemented.

## HN-20261008-001: substantive progress and bounded delivery

The observer previously treated any changed Tool output as a progress milestone,
even a timestamp-only result or a failed operation. It generated the same generic
sentence, asked the Persona packager to rewrite it, and consumed the delivery
interval before the main model could publish a real finding.

Tool completion now records technical activity only. The model owns progress
content. Authored updates are deduplicated and combined at the next available
window, within the existing text bound. Terminal close cancels pending delivery;
Persona and uncertain-transport fences remain intact.

Focused red: three behavioral failures reproduced automatic timestamp/error
reports and two discarded findings. The test timer seam alone was introduced
before that red run; no fixed behavior was used in the red implementation.

Focused green command:

```text
.venv\Scripts\python.exe -m pytest -q tests/test_her_v2_commentary.py tests/test_her_v3_runtime.py tests/test_her_v2_execution_commentary.py tests/test_search_activity.py --basetemp=.tmp/nightly-20261008/pytest-commentary-green
```

Result: 44 passed, 0 failed/skipped/deselected. This covers actual typed Persona
delivery, source ordering, coalescing, repetition, cancellation before Final,
provider commentary ingress and independent technical activity. Runtime
qualification/adoption and the actual external client canary follow separately.

Private evidence is retained in `.tmp/nightly-20261008/`. Earlier files and
uncommitted changes were snapshotted before editing and are not attributed to
this repair.

## Workbench pairing and preservation

The actual Windows service is port 5176; the existing Electron opens that service
from an independent source checkout. The service's default chat connection is
HASHI4. This batch tests/permits Call only on its HASHI3 connection.

A separate release worktree preserves every pre-existing modified/new source
file from the actual service checkout, then integrates the previously verified
Phone/Call and Session/native-component implementation. Merge resolution keeps
newer desktop input/mobile recovery and idle-polling repairs. Existing source
checkouts and user/connection/installation state are preserved. Client release,
service adoption, native acceptance and remaining physical-device evidence are
recorded independently below when observed.
