# Edge ordinary speech restoration

Owner: Frontend Connector. Layer: Functions and instance voice preferences.

## Approval and scope

On October 10 the user requested restoring HASHI4 ordinary narration to Edge
Xiaoxiao or Xiaoyi. Availability must not enable automatic narration. The earlier
repair authorization permits validation in HASHI3 and synchronization to HASHI4.
No Core migration or Agent reboot is authorized by this correction.

## Findings

The platform fallback has selected Windows SAPI on Windows since March 14.
Nineteen of HASHI4's 26 configured Agents had no saved voice preferences. The
October 9 automatic-enable regression exposed that fallback; the preceding
October 10 repair disabled automatic narration but retained the platform engine.
That did not restore the user's Edge preference.

An actual Chinese Edge probe exposed a second defect. The parent encodes its
pipe as UTF-8, but the isolated Python 3.12 helper ignores encoding environment
variables and reads Windows stdin as cp1252. The result is corrupted text or a
surrogate encoding failure before synthesis. The helper now explicitly uses
UTF-8 input and output, with strict malformed-input rejection and the existing
20,000-character limit. The optional runtime remains isolated.

## Instance restoration

HASHI4's current 26 configured Agent workspaces now explicitly select Edge:
25 Xiaoxiao and one retained Xiaoyi. Existing on/off/native modes and native
voice policies are preserved. Previously missing state is initialized with
automatic narration off. Ordinary semantic profiles are cleared so they cannot
override the explicit Chinese voice. The configured Call declaration is unchanged.

Preference updates use revision-checked configuration publication, retain raw
backups, and fail on conflict. Local identities, paths and preferences remain
ignored instance data. This operation restores the existing Agents; it does not
change the shared OS fallback or invent a new global configuration surface.

## Verification and adoption

- Focused encoding regression: four failures before repair; malformed input,
  empty input and the size limit are included. An initial oversized test-case ID
  was shortened before recording the final red result.
- Encoding, voice preference and isolated runtime scope: 27 passed in HASHI3.
- Actual Edge service through the configured isolated helper: Xiaoxiao produced
  7.464 seconds and Xiaoyi 7.632 seconds. Both OGG files fully decode and have
  nonzero signal; disabled narration produces no file. This is synthetic-text
  generation and decoding, not a claim of human listening.
- The live generation's manager rereads all restored preferences: 24 off, one
  saved TTS choice and one saved native choice. Call configuration bytes match.

Evidence is retained in HASHI4's ignored `state/edge-voice-restore-20261010`.
The real service probes use the repaired source helper with the current
generation's manager/provider in a separate check process. They do not prove
adoption in existing Agent Workers. Their helper remains the immutable old
generation until explicitly authorized hot adoption. No generation is patched
in place and no Core process is restarted.
