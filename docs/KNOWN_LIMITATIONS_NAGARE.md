# Nagare Known Limitations

- `nagare-core` ships with a deterministic smoke handler and a subprocess handler, but production-grade host integrations still need their own adapters for auth, routing, and notifications.
- The standalone CLI smoke path is intended for packaging verification, not model-quality validation.
- `nagare resume` clears the pause signal for a still-running workflow; it does
  not relaunch a stopped process or reconstruct an interrupted worker.
- Evaluation is supplied by an optional host adapter and is not advertised as
  a standalone `nagare` CLI command.
- The loopback-only API exposes read endpoints plus trusted-local endpoints that can start a
  workflow and deliver executable Python callables. It is not an authenticated network service
  and must not be proxied to an untrusted network.
- Callable code runs in-process and is not sandboxed. An executing callable cannot be safely
  pre-empted; an explicit stop is observed after it returns.
- Pause/resume uses signal files in a live process. Nagare does not reconstruct an interrupted
  worker or resume a run after process failure.
- Debug recovery rejects an identical repeated diagnosis/fix record, but it cannot prove that two
  differently worded actions are semantically distinct. A misbehaving Debug Agent can therefore
  still prolong a run until an explicit stop; its contract requires an `unrecoverable` result when
  no safe changed action remains.
- Free-form `success_criteria` and the top-level `output` block are host-facing metadata. Blocking
  requirements must be expressed as required step artifacts and automatic quality gates.
- `nagare-viz` preserves raw YAML on no-op export, but structured edits do not yet guarantee
  arbitrary comments or unknown nested fields. Use raw YAML for unsupported fidelity classes.

See [ROUND_TRIP_CONTRACT.md](ROUND_TRIP_CONTRACT.md) for the supported YAML
round-trip boundary.
