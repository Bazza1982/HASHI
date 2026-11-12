# /getfile — end-to-end Function contract

Status: **implemented on `feature/getfile`; review candidate, not production-adopted**. Non-production test/acceptance evidence is recorded in the companion PRs. External Telegram acceptance requires an independently configured test Bot/chat.
Functional owners: **PAO** (Agent-scoped recent-file projection, Run/tool provenance, current access checks) and **Frontend Connectors** (command, menu and destination-scoped media delivery). Engineering layer: **Functions**. Do not change protected Core, its runtime/ABI or a versioned Core contract.

## User-visible contract

- `/getfile` opens a compact menu of the latest **10 distinct files recorded as created, modified or touched by the currently selected Agent**, across its Runs and authorized Workzones. Order by verified/reported activity time, not filesystem mtime. Show name, shortened location, action/time and an honest coverage indicator. Missing or currently inaccessible entries are disabled with an explanation. Refresh re-queries the same owner.
- Click one item to fetch the **current bytes** (not a historical version) from the file-owning HASHI machine and deliver it to the **exact original command conversation/connector**. Do not trigger a model Run. Do not redirect to a default Telegram chat or broadcast to configured mirrors. If the file changed after menu creation, report that fact.
- `/getfile <path>` sends a named file directly; preserve spaces, quoted paths, Windows drive paths and WSL translation where supported. Workbench menu supports a bounded path text field; Telegram can use command text and optionally a time-limited path-reply flow. An explicit path outside currently admitted Workzones requires a clearly owned, authenticated host-path capability; never treat a menu token as a path grant.
- Output: Telegram **lossless document** in originating chat; Workbench canonical message attachment in the originating Session, with open/download, and explicit immediately retained **File Library** copy for rich preview (Word/Excel/PPTX etc.). Workbench and Telegram must not receive the same requested file merely because a mirror setting exists. No new public listener, shared URL or credentials.
- Surface size/transport limits explicitly. Current ordinary Session attachment cap is 64 MiB total per Message; public Telegram Bot API uploads are lower (50 MB). Bigger Workbench files need a separately bounded streamed/chunked transfer; never claim success after a partial transfer. Do not load multi-GiB files into memory.
- Enforce current owner/Agent/Instance/Session/context-generation binding at every list, click and delivery. Exact source root and OS read rights are rechecked at transfer time. Opaque short-lived callback IDs; no absolute machine paths in callback data. Handle symlinks, retargeting, move/delete, file replacement, disallowed roots, stale menus, double clicks, unknown acknowledgements and size limits.

## Evidence and recent-file coverage

Use existing owned streams rather than enabling Fingerprint or scanning an entire drive on demand:

1. `tools/registry.py` canonical `tool_call` events and confirmed `file_write`/`apply_patch` tool receipts. For exact **created/modified** claims, observe whether the target existed before the successful action and verify the effect; if not confirmed, label only `touched`.
2. `orchestrator/runtime_pipeline.py` canonical `provider_stream_event` with `file_edit`, including Codex `item.completed/file_change` paths. Normalize relative paths against the **admission-time trusted Run Workzone snapshot**, not the current cwd or a common parent. Grok/Claude adapters may lack verified path or completion; do not manufacture one.
3. Fingerprint receipts, **when already enabled**, add strong provenance/versions but are not a precondition. Shell/bash, native CLI events and background writes have explicit coverage gaps. Improve directly available provider/tool adapter events first; add a bounded active-Run filesystem observer only if needed and demonstrate no misleading agent attribution during concurrent human edits.

The index is a **small, durable derived projection** (suggest 100–200 unique records per Instance+Agent, with source ID/cursor) rebuilt from the owning audit if missing; it is not a new authoritative audit writer. Rebuild must be bounded and not read the entire growing Canonical Audit on every menu open. One Agent never sees another Agent's entries. Deduplicate by root identity + path (casefold on Windows, case-sensitive on Linux), sort by activity time, exclude internal transient/evidence files with documented rules, and preserve provenance grade. No blanket filesystem watcher while idle. Keep activity collection independent of Fingerprint ON/OFF and of Telegram connection readiness.

`orchestrator/getfile_recent.py` remains the pure selection kernel. `getfile_index.py` projects committed Canonical Audit events into `recent-files.sqlite3` (200 distinct records per owner). The latest ten are selected at query time. A bounded 4 MiB / 2,000-event tail is reconciled once per Functions process, when the index is missing, or after a marked projection failure. Recovery cannot reconstruct evidence older than that tail if the entire derived index is lost. No watcher or idle scan is installed.

## Existing integration points

- `orchestrator/commands/` and `command_registry.py`: dynamic `RuntimeCommand` + `RuntimeCallback`. Register once; command picker and Workbench catalogue derive metadata. Follow `docs/HASHI_COMMAND_UI_STYLE_GUIDE.md`, locale catalogues and server-issued callback tokens.
- `orchestrator/command_interactions.py`: Workbench capture permits `hashi_text_input` for the existing voice/call prefixes and `getfile:path:` only. The authenticated command transport accepts `input_text` only on actions, bounded to 500 characters, and canonical chat projection retains the bounded field metadata. The server-issued menu determines whether input is allowed; native callbacks receive it on `query.input_text`.
- `orchestrator/runtime_session.py:publish_frontend_media_notification_for_update`: exact Session/generation source binding and media snapshot. Integrate the completed transfer idempotently with existing PAO attachment/outbox and typed connector delivery. Review the current `configured_owner_mirror_destinations` behavior: a user-requested getfile is **origin-only** unless separately requested.
- `tools/builtins.py:_resolve_path`, `tools/device_paths.py`: reuse existing path resolution; require an explicit owner-scoped route for out-of-Workzone host paths, with the same operational authority model, not model-supplied arbitrary access.
- `orchestrator/canonical_audit.py`, `tools/effect_receipts.py`, `orchestrator/activity_digest.py`, `orchestrator/fingerprint/coverage.py`: evidence providers; avoid rescanning audit and avoid duplicate canonical state.

## Definition of done — non-production only

- Real click and `/getfile <path>` from **both** Telegram and Workbench, correct origin, file bytes/hash preserved, Workbench open/download and rich library preview; UI updates across refresh/reentry.
- Last ten are distinct, correctly ordered and durably survive Agent hot reboot; show created vs modified only when evidence supports it. Include Codex native edits and managed tool edits. Test and report gaps for Claude, Grok, shell/background, Windows/WSL paths and multi-Agent concurrency. No silent fake attribution.
- Deleted/stale/out-of-scope/broken symlink, large file, duplicate callback, lost acknowledgement, interrupted transfer, navigation away, expired menu and connector disconnect return accurate status without duplicate send or raw path leakage.
- Focused red/green evidence and cross-repo contract tests under `docs/TESTING_POLICY.md`; check protected Core changes via `python scripts/check_protected_core_changes.py --base main`. No blanket hosted CI. Run isolated/non-production real integration and real Workbench browser; production, merge and deployment require separate user authorisation.
- Keep `docs/HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md` and relevant owning contracts/tests current; root Agent FYI changes only if top-level navigation truly changes. Report source commit, tests, runtime adoption and actual UI acceptance separately.

## Implementation and delivery receipts

- `tools/recent_file_effect.py` wraps managed writes/patches with bounded before/after observations. Small file writes verify the actual UTF-8/newline bytes. Managed Shell recognizes at most eight literal output operands; interval changes remain `touched`, not a claim of Agent authorship. Native `file_edit` paths use the frozen admission Workzone snapshot. Fingerprint is optional throughout.
- `commands/getfile.py` and `getfile_service.py` bind opaque 15-minute selections to owner, Agent, instance, Session, context generation, current Workzone revision and ingress conversation. Reads pin file/parents with the existing attachment fence, reject links/reparse points, enforce regular-file/size limits and recheck identity after reading.
- One selection has one deterministic publication identity. The canonical Message, attachment and origin-only outbox are committed before transport; a repeated request recovers that publication even if the original file was deleted. Unknown Telegram acknowledgements remain uncertain and are not replayed automatically. A safely unsubmitted send follows the existing connector retry policy.
- A successful native command returns `result.getfile`: version 1, instance/Agent/source-custody identity, Session/generation, Message/attachment/event IDs, current-byte SHA-256, byte size, filename, creation time, changed-since-menu flag and delivery state. This is an authenticated command receipt; Workbench does not accept browser-supplied acquisition paths or hashes for library copies. Attachment catalogues retain their existing private-metadata boundary.
- Workbench receives an incoming attachment, plus one explicit File Library copy verified against this receipt. A failed library copy persists the source intent and retries by opaque library ID without another file request or conversation send. Background capture remains paused unless separately enabled.

## Deliberate limits

- 50,000,000 bytes for Telegram; 64 MiB for Workbench. Larger/chunked transfers are not implemented.
- Only current admitted Workzones and Agent home are accepted. There is no new out-of-scope host-path capability.
- Windows/WSL mounted-drive translation is supported. Linux-home paths on Windows require one explicitly authorized WSL UNC distribution root. Unavailable WSL shares fail closed.
- Telegram forum topics are rejected because the current FC outbox route identifies the chat rather than the topic. Use a direct Bot conversation.
- Native engines with no reliable file-edit event, indirect Shell scripts, subprocess/background writes and files beyond the bounded receipt budget remain coverage gaps. The UI labels this explicitly. `created`/`modified` are reserved for verified managed effects.
- Outer Workbench menus follow the existing in-memory command UI lifecycle; reopen `/getfile` after a worker restart. Recent evidence, delivered attachments, outbox results and File Library copies are durable.

Related Workbench consumer contract: `Bazza1982/hashi-workbench-v2`, branch `feature/getfile`, `docs/GETFILE_WORKBENCH.md`.
