# HASHI3 incremental deliverables — decision and implementation record

Date: 2026-10-02. Scope: HASHI3 branch
`feat/frontend-incremental-deliverables-hashi3-20261002`.

## Approval and owner

Barry authorized an isolated branch/worktree from HASHI3, development, and a
merge back into HASHI3 for testing. This is not Core migration or permission
to reboot any instance. PAO owns the Run, Message, attachment binding and
publication transaction. Frontend Connector owns endpoint dispatch, receipts
and transcript projection. These changes are in replaceable Functions.

## Annotation of the initial diagnosis

The confirmed first break is the ordinary `frontend_send_attachments` path:
it commits and binds managed files, but creates no visible assistant Message
or endpoint delivery task until `finish_request`. Its former “Publish” wording
described the eventual final reply, not delivery during the active Run. The
HASHI3 source has the same behavior as the fixed version cited in the initial
diagnosis. Native audio's first-ready path remains a separate exception.

The minimal design is a separate `frontend_publish_deliverable` tool. The
existing final-binding tool keeps its behavior and now says so in its schema.
One publication groups complete files, optionally adds short text, and has a
caller-supplied stable `publication_id`. Tool-call identity is deliberately not
the publication identity. The digest covers ordered file metadata and text;
same ID with changed content conflicts. The Run-wide attachment count and
byte limits remain shared with final-bound files.

Managed file stage/upload/commit precedes a single PAO transaction that
validates owner, instance, Agent, Session, current Run ID, running state and
executor fence; binds the ordered group; inserts a distinct assistant Message;
adds an `assistant.output.available` Event; associates Message and Event; and
creates endpoint tasks from the Run's frozen route. The Run stays running.
The transaction decides the race with finish/cancel: a publication committed
first survives; a later new publication is rejected. Staged bytes that never
reach this transaction do not become visible output.

For frontend consumption, an incremental Message gets its own stable
`message_ref`. The terminal assistant Message retains the existing Run-based
reference, so previous draft/final replacement behavior is preserved. The
Backend API feed and transcript expose the nonterminal output. The existing
Telegram FC media sender is awakened for its task during the tool call when
the live runtime is available. An isolated CLI gateway omits that runtime,
so the Agent's FC Worker also sweeps its own pending publication tasks and
dispatches through the same claimed endpoint renderer. The sweep continues
after a Worker replacement and marks expired claims unknown rather than
resending. Backend API, Session API, TUI and external
pull endpoints stay queued until their feed consumer accepts the Event.
HChat, Remote and Exchange currently claim only terminal events, so their
incremental media tasks fail explicitly; WhatsApp has no media egress and
also fails explicitly. The tool reports persistence and each
endpoint's current state separately. A delivered state requires its connector
proof, and unknown is not resent without the established evidence check.

At finish, only unpublished Run attachments are included in the terminal
Message. A Run with only previously published deliverables may finish
successfully without a duplicate final Message. Failure or cancellation keeps
the committed Message and managed files. This implementation does not alter
the separate external Workbench repository; its live rendering still needs
acceptance testing against the adopted HASHI3 Function generation.

## Validation and adoption boundary

Focused red evidence: before implementation, the new tool was absent from the
registry and both incremental tests failed. Focused green tests cover two
distinct publications during one running Run, stable replay and changed-content
conflict, final exclusion, a deliverable-only successful finish, Telegram
mirror proof with the Backend API endpoint still queued, stale executor
rejection, failure retention, transaction rollback/retry, and an unknown
Telegram outcome without blind resend. Legacy final-binding tests remain in
the same suite. A media-incapable WhatsApp mirror fails independently while
the Backend API task remains queued. Terminal-only HChat, Remote and Exchange
routes likewise fail explicitly. Windows cannot execute the WSL-only drive-path assertion as a
meaningful local acceptance test; that assertion is excluded from Windows
verification and its original implementation was not changed.

The final Windows source gate ran the focused attachment, Session, FC,
transcript, native-audio and HChat suites: **268 passed, 1 skipped, 3
deselected**. Two deselected audio-routing assertions also fail unchanged on
the HASHI3 base commit; the third is the WSL-only drive-path assertion. The
protected-Core check, Python compilation and whitespace check passed.

Barry separately authorized a HASHI3 hot reboot and live test. The first
adoption kept Core running and activated the changed Workers. In the live Run,
A appeared in Workbench with its file while the Run was still generating;
B appeared later, and final output contained neither file again. A and B were
published 82 seconds apart. Their Telegram endpoint tasks remained pending
with zero attempts even after final delivery, because the isolated CLI tool
gateway did not have the Worker runtime needed by the tool's immediate wakeup.
This is the observed break that prompted the Worker-owned sweep above. The
Telegram correction needs separate source validation and live adoption before
claiming the mirror is working; Workbench visibility alone cannot prove it.
