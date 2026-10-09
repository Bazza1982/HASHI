# Media defaults repair — October 9, 2026

Approval: repair and reboot/adopt HASHI3; synchronize qualified changes and
dependencies/configuration to HASHI4; user alone adopts HASHI4. Frontend
Connector, Functions/deployment; protected Core remains unchanged.

Baseline defects reproduced before repair:
- missing global audio flag hides recording despite synchronized source;
- absent Agent speech preferences resolve to off;
- absent call declaration leaves camera unavailable;
- Workbench default switch disables Call, and the local release admits only HASHI3;
- npm ignores failed media setup and omits the TTS provisioner/lock.

Focused red evidence: default config + VoiceManager: 2 failed; fresh call
configuration and malformed boundary: 2 failed; Workbench default Call: 1 failed.
These assertions exercise effective behavior rather than source-text copies.

Implementation is described in `../HASHI_MEDIA_DEFAULTS_2026-10-09.md`.
Runtime/sidecar and live acceptance results will be recorded after execution.
