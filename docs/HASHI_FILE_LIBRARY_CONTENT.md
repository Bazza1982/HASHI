# File Library presentation services

Owner: PAO Functions / SessionStore and the existing OCR/transcription services.
Workbench owns retained library membership, bytes, organization and its derived index.
These interfaces never create an Engine Run, approve an input, change a primary Session,
or make unconfirmed speech executable.

## Historical and incremental attachments

Authenticated `GET /api/v1/attachment-catalogue?limit=50&cursor=...` returns version 1,
instance identity, a signed next cursor, formal public message projections and
`history_complete`. The bounded page is owner-, instance- and authorized-Agent-scoped.
Only visible user/assistant messages in non-deleted conversation Sessions are eligible;
draft staging, internal/tool traffic and hidden results are excluded.

SessionStore schema 32 owns the durable attachment-only sequence and its INSERT/UPDATE
triggers. An older store receives one metadata backfill; reopening never requeues it.
Sequence ordering discovers a later import even when its original timestamp is old.
A SQLite read snapshot bounds each page and its high-water cursor. Cursors bind the
instance, owner and exact authorized Agent set; a changed scope requires a rescan.
A rejected or unsupported source cannot be reported as an empty completed archive.

Historical rows without a trustworthy Agent-incarnation snapshot retain explicit
`legacy-session:<id>` custody. Do not assign old messages to the current incarnation
merely because an Agent name matches. The Workbench freezes the first immutable source
occurrence and keeps every occurrence when identical retained bytes deduplicate.

`GET /api/v1/attachment-catalogue/source` requires the exact Session, message and
attachment IDs. It rechecks the authenticated owner, authorized Agent, visibility,
conversation custody and attachment membership, then returns bounded read-only
historical context around the target. It never selects that Session or enables its
live controls. Paths, inline file bytes and internal command controls stay private.
An unavailable original does not invalidate a separately retained Workbench copy.

## Optional content extraction

`GET /api/v1/file-content/capabilities` reports readiness without loading a model.
Admin-authenticated `POST /api/v1/file-content` uses one strict UTF-8 JSON body:
`filename`, `operation` (`ocr` or `transcribe`), Boolean `allow_compute`, and canonical
base64 `payload` (at most 32 MiB decoded). Metadata and bytes share the existing Remote
HMAC signature. Custom policy headers and multipart framing are not accepted. The encoded
request fits the existing 64 MiB HTTP limit; only two requests may parse/process at once. Results include the verified instance, bounded text segments, real locators,
and ready/partial status. Failures carry stable safe codes, without private paths.

OCR normalizes bounded image pixels before calling the existing OCR owner. Its native
worker is an explicit Function import dependency and launches by its qualified artifact
path in isolated Python mode. The packaging regression launches that captured helper,
so success from a mutable source checkout alone cannot qualify the deployed OCR path.
Failed extraction retains the native owner's safe reason in the diagnostic log. Transcription
uses the existing configured VoiceTranscriber and its isolated local helper. Optional
`include_segments` preserves the default legacy text protocol and returns actual
faster-whisper start/end times when requested. Compute use requires a separate explicit
body field `allow_compute: true`; the configured service must still satisfy the existing
local/LAN destination policy. No automatic cloud fallback or model selection occurs.
Text-only Compute responses remain searchable but are marked partial with no invented
time points. Native cancellation retains admission until the actual work terminates;
temporary input files are removed and late results cannot grant input authority.

## Isolated OCR provisioning

Use the approved Python 3.12.13 and the feature owner:

```text
python scripts/provision_ocr_runtime.py --runtime-dir <isolated-runtime>
python scripts/provision_ocr_runtime.py --check
```

The helper uses `constraints/ocr-py312.lock` with required hashes and a known profile
marker. It refuses Core's active interpreter and unknown preexisting environments.
It verifies actual package versions, all five pinned model sets, and their original
sizes/hashes before publishing the instance-private `state/platform/ocr.json` through
the configuration owner. Model storage is isolated with the selected runtime, not a
shared user cache. The canonical root is instance data, not necessarily the source tree.

`provision_paddle_ocr_models.py` supports the original official archive source and a
fixed-revision official PaddlePaddle Hugging Face mirror. Both must produce the same
manifest bytes. Individual model subsets allow bounded provisioning; verified files
are reused. Socket/total deadlines and partial-file cleanup prevent an incomplete
download from replacing a verified model. None of these commands updates a running
Core interpreter or automatically enables Workbench collection or media processing.

## Qualification

Run the focused catalogue/source, content-service, transcription-segment, existing
transcriber and OCR/provision tests, plus shared SessionStore/API regressions. Protected
Core and the ordinary core gate remain required before adoption. Native OCR and actual
timed speech, Functions/Agent Worker adoption, Workbench source/build/service identity,
and physical-device acceptance are separate evidence; a direct working-tree probe is
not proof that a running API adopted it.
