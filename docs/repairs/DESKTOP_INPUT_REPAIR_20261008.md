# Desktop input repair, 2026-10-08

Functional owners: PAO for manual leases and the Computer Worker; Frontend
Connector for Workbench presentation, input and error rendering. Engineering
placement: replaceable desktop Functions, the isolated Windows native adapter,
and the existing production Workbench checkout. Protected Core is unchanged.

## Authorization and continuity

The user requested urgent complete repair of click-triggered disconnection,
including lock-screen control. Prior production-Workbench adoption authority is
retained. The subsequent steer says the Windows screen is now unlocked and asks
to continue testing. Existing files, host installation, receipts and earlier
failed tests were retained. HASHI4 code/runtime were not changed or restarted.

## Causes and decisions

An ordinary interactive Worker could capture a lock picture but could not
reliably inject on Windows' secure desktop. Locked state also made its health
projection unregister an otherwise available transport. The selected HASHI3
Computer Worker now keeps available/locked transport separate from interactive
state and uses the explicitly configured, isolated local desktop host. This is
an administrator-installed platform opt-in, not a new Remote identity or a Core
import. The default unconfigured adapter remains the ordinary desktop.

The entire monitor list had been used to fence one selected view. An unrelated
monitor's power/geometry change invalidated primary-display input and released
its lease. The fence now derives from the selected monitor only. Selected-monitor
changes continue to release control and invalidate cached coordinates.

Browser timing identified the repeated unlocked click failure: hidden
conversation polling occupied HTTP/1 connections. An input request waited about
2.2 seconds before it was sent, while the actual response took about 23 ms. A
later attempt arrived with a 5.1-second-old frame and correctly failed freshness.
The corresponding direct authenticated transport checks took 7–62 ms. No route,
authentication, freshness or write-lease safety was relaxed. Workbench pauses
hidden conversation metadata/transcript/activity/feed polling while desktop is
presented, retains conversation state, and immediately hydrates through the
existing owner when returning. Network transit now counts toward displayed-frame
freshness; positional clicks avoid redundant pending moves. Brief frame gaps
pause input without blindly replaying the rejected event.

Actual public-UI observation also found that a successful picture erased an
acquisition-busy explanation. Control/input failures now remain visible until
explicit successful acquisition or reconnection. The existing Agent/manual
mutual exclusion remains: a HASHI browser operation itself holds the shared OS
device lock and cannot acquire manual control concurrently. Public acceptance
waited for that operation to release its lock before using the normal visible
control button. While manual control was held, a later Agent browser action was
correctly refused. This was a test-driver concurrency boundary, not permission
to steal another actor's lease.

## Source, adoption and observation

The selected HASHI3 Computer Worker alone was replaced through the managed
process tool and existing isolated interpreter. Its replacement adopted the
native adapter and monitor fence. The session-bound SYSTEM service is active.
No HASHI Core or other instance was restarted; no packages were installed into a
running Core. Workbench UI was built from its actual running checkout, preserving
its unrelated experience fixes. New assets were copied before atomically
replacing the index, retaining older hashed assets. Backend service identity and
chat routing remain unchanged; desktop uses HASHI3 and chat HASHI4. These are
uncommitted patches, not a clean published release.

The served production Workbench, not the 5179 candidate, delivered real Windows
input to an independent native form: left/right click counts, textbox Unicode
including a surrogate pair, physical key/shortcut events, dragging and scrolling
changed. Fullscreen, release/reacquire, reconnect and a 60-second control/frame
soak passed. Chromium's native touch protocol separately drove the phone
trackpad and visible mouse buttons, inserted Unicode, scrolled the real Windows
list, and retained control through fullscreen, landscape and reconnect. That is
emulated touch with physical OS effects, not a physical phone.

The signed-in connected Chrome at the actual public Workbench independently
loaded the final production entry and used visible Tab and Send Text controls.
The native Windows observer received the exact unique public-test text and key
events. Six ten-second observations retained control and decoded current images
without desktop errors. Browser receipts, native effects and images are separate
evidence. No credentials were entered or retained.

Focused desktop/Broker/Worker checks passed 58 cases. The curated Core gate
passed 809 with one existing platform skip when run in the selected environment
with inherited Python overrides excluded. The first broader run's qualification
failure was an inherited-environment dependency fingerprint mismatch; isolated
qualification and the full corrected run passed without code/dependency changes.
An earlier command used a different interpreter without the async test plugin;
its five collection/execution failures are retained as environment evidence and
are not called product failures. Workbench controller, conversation-transition,
desktop policy and browser/fullscreen checks passed, including the new retained
control-error behavior. No source assertion or copied prose certifies input.

## Failure proof and private evidence

Ignored HASHI3 evidence is under `state/desktop-lock-repair-20261008/`; the actual
Workbench checkout has its corresponding ignored evidence directory. The
selected-monitor red run had two failures; the fixed component checks passed.
The conversation-pause scenario failed on the safely restored baseline and
passed on the fix. The busy-control scenario failed because a frame erased the
error, then passed with retained failure state. Original stale-frame and old
lock-preview/character receipts were preserved rather than overwritten.

The actual local pipe boundary also rejected another session, arbitrary
operation, out-of-display capture and oversized envelope. A real counterfeit
user-owned pipe received zero request bytes, proving authentication precedes
dispatch. The real host stayed available after rejection.

The final FYI check exposed an existing over-budget reference: its 25,095
characters were truncated before later owner rules. The entire prior file was
archived byte-for-byte in `docs/AGENT_FYI_HISTORY_DESKTOP_20261008.md`; the active
reference retains owner/security rules and the current desktop outcome in
11,921 characters. PCM owns this documentation/reference correction; no loader
code or budget was changed. The existing real primer tests went from one failure
and two passes to three passes, confirming full untrimmed rendering and request
preservation. Historical receipts retain their original scope in the archive.

Early mobile driver attempts used synthetic pointer IDs, tried to resize a
native fullscreen window, or assumed repeated text always inserts at its end.
Those driver failures are retained separately. The final touch driver uses
browser-native touches and independently verifies a new inserted sentinel;
they are not product-defect red evidence.

## Remaining acceptance and rollback

Earlier lock-screen click and masked-character/backspace observations establish
individual secure input only. Complete login with the user's actual credentials,
the physical phone's browser/network, and extended resource/performance acceptance
remain separate. The unlock steer did not authorize relocking the computer as a
test shortcut. Do not report these repairs as a perfect or complete device
certification. HN-20261006-004 can record unlocked physical-input progress but
retains the physical-phone/login boundary. HN-20261004-004's separate HASHI4
transport issue and Phone/Call items are not closed by this work.

Rollback only the desktop-owned changes and selected Worker/platform opt-in.
The optional host must be stopped through its service owner before any host
replacement; process termination uses HASHI's managed process tool. Preserve
unrelated Workbench changes, user state and other instances. Final Core/source
checks and deployment hashes remain in the private manifest.
