# Files, attachments and media

## Purpose and when to read
Read for attachment binding/retention, file library discovery, extraction, image/audio input, vision and authorized device media.

## Ownership and layer
PAO owns scoped resources, Workzones and attachment lifecycle in Functions; Frontend Connectors transport and render projections. PCM may project admitted references without owning files or authorizing extraction.

## Distinctions
Attachment ownership, integrity and access are separate. A source locator does not switch the primary Session. OCR/transcription require the applicable explicit setting and real provenance; image previews are not vision inspection. Remote transport never expands authority.

## Authoritative documents
- [File library content and extraction](../HASHI_FILE_LIBRARY_CONTENT.md).
- [Attachment and transport contract](../HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md).
- [Native audio and media provenance](../HASHI_NATIVE_AUDIO_CHAT_DESIGN.md).
- [Workzone/Session boundary](../HASHI_AGENT_WORKZONE_SESSION_BOUNDARY_FIX_2026-09-27.md).
- [Device control and stale input](../HASHI_CROSS_PLATFORM_DEVICE_CONTROL_PLAN.md).

## Source and verification
Start with the owning resource modules imported by `orchestrator/workbench_api.py`, `orchestrator/compute_vision.py` and the relevant media tool. `tests/test_session_attachment_authorization.py` protects scoped access; `tests/test_compute_vision.py` covers vision service admission. Inspect the actual owning tests for retention, signed cursors, extraction and failure boundaries.

## Adoption
Keep file availability, verified integrity, extraction results, runtime adoption and device observation distinct. Model availability does not grant access to private files. Preserve Session/Agent/Run identity and do not expose source paths or credential content in diagnostics.
