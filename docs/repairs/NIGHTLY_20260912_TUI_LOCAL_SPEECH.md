# Nightly repair: TUI-local reply speech

## Corrected ownership

TUI `/say` is now intercepted by the Frontend Connector. It reads the last
final Agent reply visible in the selected TUI scope, requests TTS from that
Agent's instance, validates the returned Ogg size and SHA-256, and plays it on
the computer that launched the TUI. It never invokes the Telegram `/say`
handler, calls a Bot, enqueues a Message, or changes auto-read.

`/voice on|off` is a local preference keyed by launch client, instance and
Agent. Profile choices are dynamically read from the selected Agent's existing
voice owner; changing one writes the shared Agent profile. `/voice advanced`
keeps provider/model/format/fallback/retention information outside the compact
daily menu. `/tui sound` remains an independent short-cue setting.

## Lifecycle and transport

Direct and authenticated Remote paths use dedicated `voice_state`,
`voice_profile`, and `speech` operations. Remote returns actual bounded audio,
not a filesystem path. The TUI uses one lock-protected player task. A newer
request cancels the old one; Agent/instance changes, auto-read Off, disconnect,
or exit discard pending/late audio. Successful message identities are retained
in a bounded local set so transcript redelivery cannot auto-play twice.

The platform adapter uses Windows MediaPlayer on native Windows/WSL, `afplay`
on macOS, or an available `ffplay`/PulseAudio/ALSA player on Linux. Temporary
audio is private and deleted after completion or cancellation.

## Persistence and validation

The Agent `voice_state.json` owner now uses the common BOM-compatible,
UTF-8/LF, private atomic, revision-checked writer. Mutations fresh-read and
report conflicts without replaying the setting action; corrupt state is never
overwritten and unrelated fields are preserved.

Focused tests cover direct/Remote field routing, generated-asset integrity,
TUI-only `/say`, local preference reopening, duplicate suppression, shared
profile selection, old Telegram command regression, worker proxy consumption,
legacy encoding, and corrupt-state rejection. Physical playback is deliberately
left to the user-authorized listening check; automated validation replaces the
player function and produces no sound.
