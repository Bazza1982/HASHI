# Telegram media admission repair, 2026-10-10

## Scope and cause

The user requested diagnosis and repair on HASHI3 and HASHI4 after the Telegram
photo request failed with MEDIA_PATH_NOT_AUTHORIZED. HASHI3 reboot is allowed;
HASHI4 reboot remains manual. Functional owner: Telegram Frontend Connector,
using PAO Session attachment APIs. Engineering layer: Functions. Core unchanged.

The failed request stopped before provider invocation. Its persisted original
user Message contained only text describing a downloaded photo; it contained
no committed attachment reference. The Telegram single-media and /long paths
still constructed synthetic attachment IDs and local-file references. The
current-Run attachment fence correctly rejected these unregistered IDs.
HASHI3 and HASHI4 had identical affected ingress and authorization source.
This is separate from in-run answer handling: the user's preceding Telegram
choice was received and consumed successfully.

## Resulting behavior

The narrow Telegram media adapter validates bytes against download receipts
within the Agent's download directory, stages/uploads/commits through PAO,
derives canonical parts, and binds the resulting IDs to the original Message.
The resolved Session, owner and generation are carried into queue admission.
Single photos, documents, videos and completed /long batches share the adapter.
Batch order and captions remain intact; integrity failures remain visible.
Text-only batches retain their existing path. Native voice keeps its own
admission, transcript gate and retention policy.

No Engine authorization check is relaxed. Another Message's attachment, even
in the same Session, remains unauthorized for the current Run. Raw files are
not grandfathered into old failed Runs, and no failed request is auto-replayed.

## Verification

Red: four real Telegram handler -> SessionStore admission -> backend consumption
scenarios reproduced the exact MEDIA_PATH_NOT_AUTHORIZED error: single photo,
document, video and a two-photo /long batch. Green: all four pass after repair,
including committed Message references and rejection of another Message's
attachment.

Focused command: python -m pytest -q tests/test_session_attachment_authorization.py
tests/test_runtime_media.py tests/test_runtime_long.py tests/test_native_audio_chat.py

Result: 119 passed, 2 skipped. The skips are existing platform/optional cases,
not passes. This scope covers existing cross-owner/Agent/Run fences, integrity
and filesystem replacement checks, native voice and grouped delivery.
An intermediate run exposed a timing regression from offloading legacy
text-only batch handling; removing that unnecessary offload restored the
existing behavior. The final result above includes this correction.

The regression uses real PAO persistence and backend validation with a fake
Telegram download transport and a byte-consuming test backend. It is not a
claim of human Telegram-client or real-model image acceptance.

## Deployment

Implementation and checks are complete on HASHI3. Running adoption and HASHI4
source deployment are recorded separately after qualification. HASHI4 manual
reboot and the user's Telegram screenshot resend remain the production live
acceptance step; no HASHI4 reboot is authorized or performed.
