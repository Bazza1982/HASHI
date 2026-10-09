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

Implementation checkpoint: HASHI3 ab3cc244; HASHI4 cherry-pick 33232569.
The three production modules and regression test have identical source (one
new file has only the normal checkout line-ending difference).

HASHI3's existing idle Codex canary Agent adopted the committed Function
generation through a targeted hot reboot. The live generation manifest names
ab3cc244 and includes telegram_media_admission. An isolated Session then
uploaded a two-colour PNG through the real Session API. The actual Codex
app-server image turn completed with "MEDIA-ACK: red, blue", with no backend
error. This proves the deployed Worker can consume a real registered image;
the Telegram handler-to-Run binding is covered by the focused regression.
It does not claim a physical Telegram client resend. Other HASHI3 Agents were
not rebooted by this narrow canary.

The same focused scope on HASHI4 source passes: 119 passed, 2 skipped. It used
the HASHI3 test interpreter, without modifying the live HASHI4 environment.
Both checkouts pass the protected-Core check. HASHI4 source is deployed; its
running Workers were not rebooted. The user must manually adopt the source
before resending the failed Telegram screenshot. A whole-instance hot reboot
adopts it for all Agents; a targeted reboot adopts it only for that Agent.

Ignored operational receipts: state/telegram-media-repair-20261010 contains the
adoption receipt, generation operation receipt, actual PNG, completed model
Run receipt and HASHI4 focused JUnit result. No tokens are stored in these
receipts. The original failed Run remains unchanged.
