# HASHI3 desktop view recovery, 2026-10-08

The later unlocked-input repair and independent production/public-input
observations are recorded in
[the input follow-up](DESKTOP_INPUT_REPAIR_20261008.md). They supersede the older
unverified-input status below, while preserving its original viewing evidence.

Owner: Frontend Connector. Layer: Workbench instance startup and operational
availability. Scope: HASHI3 and its paired Workbench candidate, not HASHI4 or
production Workbench publication. The user explicitly requested immediate
HASHI3 repair for testing; the earlier night-only timing no longer applies to
this desktop-view recovery.

## Fault and recovery

The paired Workbench listener was stopped. HASHI3's Backend API listener and
Core process were also absent, although HASHI Remote was still running.
Consequently desktop discovery could not reach a live HASHI3 desktop broker.
Mouse and keyboard injection failures do not explain these missing listeners.
The original cause of their termination has not been established.

The existing paired Workbench build was started with its existing HASHI3
connection, secret reference, installation state and physical desktop binding.
The ignored launcher now explicitly sets the desktop opt-in. The stored desktop
configuration was already enabled; missing opt-in was not established as the
cause of the stopped listener. No other Workbench source or build was replaced.

The already stopped HASHI3 instance was started through its normal entry and
qualification gate. The first recovery attempt inherited HASHI4's PYTHONPATH
from the calling environment and failed the dependency fingerprint comparison.
The next launch used Python's standard `-E` option, excluding inherited Python
environment overrides. Qualification then passed without dependency changes,
Core edits or bypassing fingerprint checks. This operator-launch failure is
separate from the original service termination.

## Current live evidence

Workbench candidate: `9edaa033`, HASHI3 source: `129f6cad`.
The test entry is loopback port 5179, layout Virtual Desktop. It uses the
authenticated Remote hop to HASHI3, independently bound to the physical host.

Subsequent production authorization on 2026-10-08: the actual Windows Workbench
service on port 5176 now adopts the desktop-only recovery. Its existing checkout
and unrelated experience fixes were preserved. The production served page was
observed independently: valid monitor JPEGs, reload, reconnect and a mobile-sized
viewport passed; active chat stayed HASHI4 while the bound local desktop used
HASHI3's authenticated transport. The main monitor shows the Windows lock screen
and secondary monitor images are black. Workbench-only service replacement did
not alter or restart HASHI4. This closes the production desktop-view publication
gap, not the current physical-input or external/phone acceptance gaps. Private
production evidence is in its ignored state/desktop-production-20261008 folder;
the owning record is docs/implementation/desktop-production-recovery-20261008.md
in that Workbench checkout. The original 5179 evidence below remains scoped to
the earlier test recovery.

The final browser observation exercised the served build and real API:

- Three physical monitors decoded real JPEG images in the visible desktop
  surface. The observed screens were DISPLAY2, DISPLAY1 and DISPLAY4.
- Page reload and the visible reconnect action opened fresh view sessions and
  rendered new images successfully.
- A 412-by-915 browser viewport displayed the actual image. This proves the
  mobile layout, not a separate phone or external-network connection.
- No desktop control acquisition or input operation was required or sent.
- The final observation had no page errors. Screenshots visibly show the
  current Windows lock screen; this is not proof of successful login or input.

Evidence remains separate in the ignored `desktop-emergency-20261008` directory:
`desktop-direct-frame.json`, `desktop-direct-frame.jpg`, `desktop-view-live.json`,
the three monitor screenshots, and `desktop-mobile.png`. Earlier observer
failures are retained as separate records. One recorder attempted to read an
empty/interrupted browser-response body; another required a new full JPEG when
reselecting an unchanged monitor, which correctly reused the existing image.
Neither failed recorder is used to certify the final rendered-view observation.

## Remaining boundaries

Later production follow-up: the actual Workbench 5176/public ingress now adopts
the phone/fullscreen/error fixes. Served WebKit/Chromium phone contexts passed
real-image, portrait/landscape, fullscreen exit, touch controls and reconnect.
Signed-in Chrome separately decoded a real public-ingress frame from the same
new served build. This extends viewing/browser evidence only: Windows remains
locked and physical-phone input remains pending. No HASHI runtime/code change
was needed. The separate HASHI4 transport defect is not closed. See the owning
production Workbench docs/implementation/desktop-mobile-external-repair-20261008.md
and its ignored state/desktop-mobile-20261008 evidence. Earlier scope below is
retained as historical context.

Actual mouse/keyboard acceptance remains open. The user may test HASHI3 now.
Production Workbench's desktop opt-in and HASHI4 adoption remain separate work;
this HASHI3 recovery must not be reported as publication to the usual production
entry. Existing Workbench fixes and production services were preserved.

The nightly inbox links this recovery to HN-20261006-004 and HN-20261004-004.
Phone/Call production deployment remains in HN-20261008-002 and is not certified
by desktop-view evidence.
