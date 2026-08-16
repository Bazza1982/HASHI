# HASHI Execution Runtime source

This directory contains the Rust source used by the development-only
`/rebuild` workflow.  The source was imported from the certified HER
`0.1.0-hashi.22` release; exact provenance is recorded in
`UPSTREAM_SOURCE.json` and the upstream MIT license is retained as `LICENSE`.

Development builds are immutable candidates under the instance bridge home.
They do not replace or mutate `packages/her/*`, its manifest, or its certified
release binaries.  Promotion of a candidate into a packaged HER release remains
a separate reviewed release workflow.
