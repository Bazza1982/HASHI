# Telegram Notification Modes

Status: **active operational contract**

`/notify` controls Telegram notification signalling for one Agent workspace. It
does not suppress, discard, delay, or hide messages.

## Choose the right control

| Intent | Control | Scope |
|---|---|---|
| Change notifications for delivered Telegram messages | `/notify [on\|quiet\|off]` | One Agent's workspace. Off still delivers messages silently. |
| Change the ordinary mirror preference | `/telegram [on\|off]` | The invoking owner; existing Agent mirror policy can apply. |
| Stop or restore mirroring across this instance | `/telegram off global` / `/telegram on global` | All Agents, including those set to always mirror. Direct Telegram conversation continues while safety silence is off. `/telegram global` shows the state. |
| Cut Telegram traffic in both directions across this instance | `/telegram silent global` | Instance-wide safety silence. Use `/telegram silent off global` from a connected non-Telegram frontend to lift it. `/telegram on global` restores mirroring and also lifts safety silence. |
| Remove the locally stored Bot Token and stop connecting | `/telegram disconnect [agent_id]` or Workbench's Telegram settings | The selected Agent. A shared credential cannot be removed from one Agent. The Telegram platform token itself is revoked separately in BotFather if needed. |

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
or Function Worker contract mismatch must never turn into a silent delivery failure.

## Catch-up eligibility

Notification mode controls signalling, not automatic catch-up eligibility.
FC separately limits replay to final replies/reports/files for 2 hours and
genuine failures/command results for 10 minutes; pending questions also retain
their own shorter deadline. Transient displays and normal user interruptions
are never replayed. Catch-up identifies itself and the original UTC time.
See [the FC recovery contract](HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md#bounded-telegram-catch-up-2026-11-12).

## Sound and vibration

HASHI sends Telegram's Boolean `disable_notification` field. The Bot API does
not provide independent controls for sound versus vibration. When HASHI sends a
normal notification, the Telegram app and the phone's per-chat, per-app, sound,
vibration, Focus/Do Not Disturb, and operating-system settings decide whether
the device plays a sound, vibrates, does both, or does neither.

Therefore “sound but no vibration” is not a separate HASHI notification type.
It normally reflects Telegram or device settings.

## Reload contract

The notification provider module is a Function generation foundation dependency. Both
targeted `/reboot min` and full `/reboot max` validate that the current
three-mode helpers, purpose-aware delivery policy, and `/notify` command are
loaded before accepting the refreshed runtime.
