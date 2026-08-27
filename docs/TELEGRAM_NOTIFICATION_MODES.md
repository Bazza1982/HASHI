# Telegram Notification Modes

Status: **active operational contract**

`/notify` controls Telegram notification signalling for one Agent workspace. It
does not suppress, discard, delay, or hide messages.

## Modes

| Mode | Command | Telegram behaviour |
| --- | --- | --- |
| On | `/notify on` | Every message uses normal Telegram notification signalling. |
| Quiet | `/notify quiet` | Interim activity is silent. Final results, command/background completions, errors, warnings, recovery notices, control messages, and important alerts notify normally. |
| Off | `/notify off` | Every message is delivered with Telegram's silent-message flag. |

Running `/notify` without an argument opens the same three-state menu. The
legacy `on`/`off` command forms and persisted `.notify_on` marker remain
compatible. Quiet persists as `.notify_quiet`; the two markers are mutually
exclusive. With neither marker present, the default is Off.

Quiet treats acknowledgements, Persona commentary, reasoning presentation,
technical `/verbose` activity, typing/placeholder updates, answer previews,
and meter/meditation progress as interim. A final answer split into several
Telegram messages keeps all earlier chunks silent and notifies only on the last
chunk.

Notification policy is fail-open for delivery: if policy evaluation fails, the
message is still sent and defaults to normal notification signalling. A policy
or hot-reload mismatch must never turn into a silent delivery failure.

## Sound and vibration

HASHI sends Telegram's Boolean `disable_notification` field. The Bot API does
not provide independent controls for sound versus vibration. When HASHI sends a
normal notification, the Telegram app and the phone's per-chat, per-app, sound,
vibration, Focus/Do Not Disturb, and operating-system settings decide whether
the device plays a sound, vibrates, does both, or does neither.

Therefore “sound but no vibration” is not a separate HASHI notification type.
It normally reflects Telegram or device settings.

## Reload contract

The notification provider module is a hot-reload foundation dependency. Both
targeted `/reboot min` and full `/reboot max` validate that the current
three-mode helpers, purpose-aware delivery policy, and `/notify` command are
loaded before accepting the refreshed runtime.
