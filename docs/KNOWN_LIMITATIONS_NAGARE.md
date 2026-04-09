# Nagare Known Limitations

- `nagare-core` ships with a deterministic smoke handler and a subprocess handler, but production-grade host integrations still need their own adapters for auth, routing, and notifications.
- The standalone CLI smoke path is intended for packaging verification, not model-quality validation.
- Resume and control operations in `nagare.cli` remain limited; `resume` is still a placeholder.
- The API is intentionally read-only in this phase. Job control is not exposed over HTTP.
- `nagare-viz` currently focuses on read/build readiness; broader editor fidelity work is still downstream.
