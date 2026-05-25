# HASHI v1.1 — Release Notes

Release focus: **stability + correct session semantics**, with a clear documented path to v2.

---

## Highlights

- **/new is now truly bare (stateless)**
  - No Bridge FYI injection.
  - No automatic README/docs reading.
  - Agents follow only their workspace `agent.md` instructions.

- **Post-release remote and operator fixes landed**
  - Simplified `/remote` so the main status view also shows connected peers.
  - Corrected remote peer host selection and route fallback behavior.
  - Fixed Telegram callback length failures in `/jobs` and `/nudge`.
  - Added HASHI2 watchdog validation for the remote stability window.
  - See `docs/HASHI_REMOTE_FIX_BUNDLE_2026-05-26.md`.

- **Documentation refresh**
  - Consolidated active docs and moved historical plans into `docs/ARCHIVE/`.
  - Added a single troubleshooting entrypoint: `docs/TROUBLESHOOTING.md`.

- **v2 roadmap defined**
  - See `docs/ROADMAP.md` for the v2 target outcomes.

---

## Roadmap to v2 (pointer)

- `docs/ROADMAP.md`

---

## Notes

This release note is intentionally high-level. For the complete history, see `CHANGELOG.md`.
