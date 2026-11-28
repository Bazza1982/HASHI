# HASHI migration and retired features

This page describes the v6.0.0-alpha.1 source on `main`. Check the exact
program and Worker versions on the instance before applying it. Published
packages and older deployments may still expose the previous features.

| Retired surface | What to use or keep |
|---|---|
| Gemini CLI engine (`gemini-cli`) | Use [Antigravity CLI or a supported Model Provider](HASHI_GEMINI_CLI_RETIREMENT.md) for Gemini models. Existing transcripts remain readable. Antigravity has its own login and session. |
| Native `xai-api`, `xai_imagine`, `/xaiauth`, `hashi.py auth xai`, and Gateway `/v1/images/generations` / `/v1/videos/generations` | Use Grok CLI for Grok. Update API clients that target the removed routes or use a retired default model. Select an available model in `/api`, or send an explicit model from `GET /v1/models`. Historical xAI billing identifiers remain. Use a configured media tool for supported media work. [#250](https://github.com/Bazza1982/HASHI/pull/250) |
| Hermes transfer command and `scripts/hermes_transfer.py` | The `.hashi-hermes-agent` profile import/export procedure is historical. Preserve existing packages and review them with the version that created them. HASHI-to-HASHI [Agent Move](HASHI_AGENT_MOVE_V1.md) and Remote/HChat are separate capabilities. [#249](https://github.com/Bazza1982/HASHI/pull/249) |
| Anatta observer, `/anatta` and its diagnostics | New releases do not run the observer. Existing declarations are preserved and reported as unavailable rather than blocking Function qualification. Old Anatta memory rows remain covered by the existing forget cleanup. [#249](https://github.com/Bazza1982/HASHI/pull/249) · [#253](https://github.com/Bazza1982/HASHI/pull/253) |
| JEV Judge and experimental Agent Companion | HERV3 uses one main model/tool loop. Legacy `agent_companion` settings are ignored. Progress updates, the long-tool keepalive, optional Strategy Cards and Habits remain available on their supported paths. [#249](https://github.com/Bazza1982/HASHI/pull/249) · [#250](https://github.com/Bazza1982/HASHI/pull/250) |
| Staged HER routing and Quick/Pro/Hybrid controls | Select an engine, provider, model and supported reasoning level. Legacy identifiers such as `her-v2` and stage labels may remain readable for persisted configurations and checkpoints. Native `her-v3` is the public engine ID. [#240](https://github.com/Bazza1982/HASHI/pull/240) · [#260](https://github.com/Bazza1982/HASHI/pull/260) |
| `veritas/` and its Nagare callable registration | The retired handlers are `mineru-extractor`, `knowledge-block-assembler` and `vault-writer`. Review private workflows that reference them before upgrading. Nagare, Office tooling and the privacy detector remain separate supported surfaces. [#248](https://github.com/Bazza1982/HASHI/pull/248) |
| The original Workbench bundled in HASHI | Install and maintain Workbench V2 separately through its own repository/distribution. HASHI retains the client APIs and [compatibility names](WORKBENCH_NOTES.md). |

## Before switching an existing installation

1. Record the existing version, instance identity and running build. Back up the
   instance data; release preparation and runtime adoption are separate actions.
2. Review Agent engine settings, the Gateway's saved model and any scripts,
   Workflows or API clients that use a retired command or route. Select a
   supported replacement in the owning settings or update the external client.
3. Use [the source adoption guide](HASHI_SOURCE_RELEASE_CLOSURE.md) for a source
   install and [the installation guide](INSTALL.md) for a published package.
   Verify the actual serving generation with `/version full` and the relevant
   lifecycle receipt after adoption.

See [the current user guide](USER_GUIDE.md) and
[recent main-branch changes](../CHANGELOG.md#unreleased).
