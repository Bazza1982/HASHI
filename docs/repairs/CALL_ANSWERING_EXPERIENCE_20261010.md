# Call answering experience and voice localization — 2026-10-10

## Approval and ownership

The user approved a new HASHI3 repair round, focused testing, and source
synchronization to HASHI4. The requested scope is: localized Call voice labels
and gender, prerecorded voice previews, symbolic phone sounds, a proactive Agent
greeting when a direct `/call` is answered, and spoken rather than written
presentation throughout the call. The user explicitly rejected any rule that
makes Call responses short; long stories, detailed explanations and extended
discussion must remain available.

Functional owners:

- Frontend Connector: voice settings, preview publication, browser phone sounds
  and Call-window lifecycle.
- PAO Call Function: reserve the Call and own a once-only opening turn.
- PCM Call context: effective Persona, same-Session continuity and spoken
  presentation during the Call.

Engineering layers are replaceable Functions, instance configuration and the
external Workbench client. Protected Core is unchanged. HASHI4 source
synchronization was approved; a HASHI4 runtime reboot was not.

## Observed defects

HASHI3 already contained the localized voice catalogue and preview bundle, but
HASHI4 did not contain the two catalogue/preview modules, any of the 121 bundled
preview files, or the renderer changes. The production screenshot therefore
showed raw English labels such as `Achernar · Soft`.

Workbench stopped the connecting sound as soon as the start request returned.
A fast backend therefore made the symbolic dial/ringback inaudible. Direct Call
also waited for the caller's first utterance instead of asking the selected
Agent to answer the call.

The existing Call prompt imposed one or two short sentences and numeric length
targets. That changed substance as well as presentation and conflicted with the
requested conversational behavior.

## Resulting behavior

- In a Chinese UI, all 30 Gemini voices show `女声` or `男声` and a Chinese
  style description. Provider voice identifiers remain unchanged. English UI
  labels remain English.
- Selecting a Call voice persists through the existing revision fence and
  publishes the corresponding prerecorded sample. The package contains Chinese
  and English samples in OGG and MP3: 120 audio files plus one manifest.
- Starting a Call reserves the server call, then creates a once-only opening
  turn with sequence zero. The selected Agent receives the effective Persona,
  same-Session context and an explicit fact that the user is calling. It returns
  a natural greeting through the existing tool-free speech path. This does not
  fabricate a user Message or Run.
- Workbench keeps the symbolic dial/ringback sound active for at least 1.4
  seconds while the opening is prepared, plays the connected cue, speaks the
  greeting once, and only then opens the microphone. Confirmed start, turn,
  snapshot and speech failures use the failure cue. Confirmed hangup uses the
  end cue.
- Call presentation uses ordinary spoken sentences, transitions and pacing,
  without headings, tables, bullet lists or Markdown. Length follows the user's
  request and the subject. It explicitly permits long stories, detailed
  explanations and extended discussion for as long as needed.
- The existing Call tombstone removes Call-only presentation from the next
  non-Call turn after hangup, so later text chat returns to its normal style.

## Focused evidence

Red checks were recorded before implementation: the service returned no opening
turn; the PCM context still contained the one-or-two-sentence constraint; and
the browser stopped the connecting sound before a perceptible dialing interval.

Green checks:

- HASHI3 Frontend Call suite: 131 passed.
- HASHI4 synchronized source, verified with the HASHI3 development interpreter:
  131 passed.
- Clean Workbench Call suite: 68 passed.
- Clean production build at Workbench source `85d31b2`: passed.
- The 121-file preview asset set is byte-identical between HASHI3 and HASHI4.
- Protected Core checks pass in both repositories.

The public Workbench serves `assets/index-DZt5DdF6.js`; its active service
worker and controller both report build `85d31b28f8f0`, with no waiting
generation. Browser inspection measured the real Call surface at approximately
390 by 640 pixels and observed the dialing state and confirmed hangup through
the public entry. This is visual/protocol evidence, not a physical-device
hearing test.

## Source, publication and adoption

HASHI3 backend implementation is committed at `87a78b6f`. Workbench behavior
is committed at `700ade0`, followed by deterministic playback-test timing at
`85d31b2`. HASHI4 source and all preview assets are committed at
`e6a06ae9`.

The Workbench static client is live without restarting HASHI. HASHI4 shared
Functions have not been replaced, so the localized backend menu and proactive
Agent greeting are source-complete but not yet live in HASHI4. Supported
adoption is the user's separately authorized `/reboot max`; a targeted Agent
reboot does not replace the shared Call owner. No HASHI runtime or Core process
was restarted during this repair.
