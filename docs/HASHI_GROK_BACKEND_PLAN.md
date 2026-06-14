# HASHI Grok CLI Backend Support

**Status:** implemented and enabled as part of the HASHI support ecosystem.

**Date:** 2026-06-14.

**Goal:** operate xAI Grok Build as a first-class HASHI CLI backend, using the official authenticated `grok` CLI path with structured streaming output, backend switching, preflight detection, and guarded empty-answer handling.

**Non-goal:** install Grok Build, authenticate to xAI, or require `XAI_API_KEY` for normal HASHI use. The operator authenticates the CLI out-of-band, like Codex and Claude.

---

## 0. Current Implementation Summary

Grok CLI is now part of HASHI's supported backend ecosystem:

- Engine ID: `grok-cli`.
- Adapter: `adapters/grok_cli.py`.
- Default model: `grok-composer-2.5-fast`.
- Additional model: `grok-build`.
- Config command: `global.grok_cmd`, defaulting to `grok`.
- Backend picker: registered in `orchestrator/flexible_backend_registry.py`.
- Adapter registry: registered in `adapters/registry.py`.
- Preflight: included in `orchestrator/backend_preflight.py`.
- Answer streaming: supported via `--output-format streaming-json` text events.
- Empty-answer hardening:
  - classifies pure `thought` + `end` + no text as `thought_end_no_text`;
  - clears the Grok session and retries once only for side-effect-free empty answers;
  - refuses retry when side-effect events are present, reporting `side_effect_events_no_text`.

### Validation Snapshot

Completed on 2026-06-14:

- Unit and integration-focused checks:

  ```text
  pytest -q tests/test_grok_cli_adapter.py \
    tests/test_answer_stream_capabilities.py \
    tests/test_model_catalog.py
  ```

- `tests/test_grok_cli_adapter.py`: 8 targeted Grok adapter tests passed after the empty-answer hardening.
- Nana live controlled probe after reboot:
  - Prompt A: `Reply exactly: OK` passed.
  - Prompt B: normal summary passed.
  - Prompt C: retry-risk/safety prompt passed.
  - Prompt D: safety refusal prompt passed.
- The real empty-answer retry path is covered by unit tests; the post-reboot live probe did not trigger an empty-answer retry, so that remains a residual live-observability note rather than a blocker.

### Operational Status

Grok CLI can now be selected for flex agents that include `grok-cli` in `allowed_backends`:

```text
/backend grok-cli
/model grok-composer-2.5-fast
```

Use Grok for controlled human or reviewed agent work first. Because Grok Build is still young and can expose tool/file/shell events, agents using it should keep conservative workspace access scopes until a project-specific smoke test is complete.

---

## 1. Online Research Summary

Official xAI sources now describe **Grok Build** as an early-beta coding agent and CLI.

Key facts from official docs and announcements:

- Install command:

  ```bash
  curl -fsSL https://x.ai/cli/install.sh | bash
  ```

- Access is advertised for SuperGrok and X Premium Plus users.
- First launch can authenticate through a browser. HASHI should treat Grok like Codex and Claude: the operator authenticates the CLI out-of-band, then HASHI runs the authenticated command.
- The command name is `grok`.
- Interactive usage:

  ```bash
  cd your-project
  grok
  ```

- Headless usage:

  ```bash
  grok -p "Explain this codebase"
  grok -p "Explain the architecture" --output-format streaming-json
  ```

- Headless flags include:
  - `-p, --single <PROMPT>`
  - `-m, --model <MODEL>`
  - `-s, --session-id <ID>`
  - `-r, --resume <ID>`
  - `-c, --continue`
  - `--cwd <PATH>`
  - `--output-format plain|json|streaming-json`
  - `--always-approve`
  - `--no-alt-screen`
  - `--no-auto-update`
- `streaming-json` emits newline-delimited incremental events.
- ACP mode is available through:

  ```bash
  grok agent stdio
  ```

  In ACP, assistant text arrives through `session/update` chunks where `sessionUpdate == "agent_message_chunk"` and `content.text` contains the chunk.
- Logged-in Grok CLI `0.2.51` currently reports:
  - `grok-composer-2.5-fast`: default model.
  - `grok-build`: available coding model.
- Current `--output-format streaming-json` emits direct events such as `{"type":"thought","data":"..."}`, `{"type":"text","data":"..."}`, and `{"type":"end","sessionId":"..."}`.

Sources:

- https://x.ai/cli
- https://x.ai/news/grok-build-cli
- https://docs.x.ai/build/overview
- https://docs.x.ai/build/cli/headless-scripting
- https://docs.x.ai/developers/models

---

## 2. Implemented Architecture

HASHI supports Grok as a CLI-authenticated backend only:

- Engine name: `grok-cli`.
- Uses the official `grok` command in headless mode.
- Authentication is out-of-band, like `codex-cli` and `claude-cli`.
- Parses `--output-format streaming-json` and emits `KIND_TEXT_DELTA` for final answer chunks.
- ACP (`grok agent stdio`) can be considered later as an alternate CLI transport, not as an API fallback.

---

## 3. Backend Capability

Current `grok-cli` capability is:

```python
BackendCapabilities(
    supports_sessions=True,
    supports_files=True,
    supports_tool_use=True,
    supports_thinking_stream=True,
    supports_headless_mode=True,
)
capabilities.supports_answer_stream = True
```

The `supports_answer_stream=True` claim is backed by the parser tests and live controlled probe. If a future Grok CLI release changes event shape, HASHI should fail with explicit diagnostics rather than silently returning empty success.

If `streaming-json` proves unreliable, the only planned fallback is still CLI-based:

- Use ACP `grok agent stdio`, parse `session/update` with `agent_message_chunk`.

---

## 4. Implementation Files

Core backend registration:

- `adapters/registry.py`
  - Maps `grok-cli -> GrokCLIAdapter`.
- `orchestrator/flexible_backend_registry.py`
  - Includes `grok-cli` in `CLI_ENGINES`.
  - Includes registry entry:

    ```python
    "grok-cli": {
        "label": "grok",
        "models": ["grok-composer-2.5-fast", "grok-build"],
        "default_model": "grok-composer-2.5-fast",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [],
    }
    ```

- `orchestrator/config.py`
  - Includes `grok_cmd: str = "grok"` in `GlobalConfig`.
  - Loads `global.grok_cmd` from `agents.json`.
- `orchestrator/backend_preflight.py`
  - Includes `grok-cli` command availability checks.
- `orchestrator/model_catalog.py`
  - Optional later: expose Grok models through the OpenAI-compatible local API gateway if CLI-backed gateway routing is desired.
- `agents.json`
  - Selected agents can include `grok-cli` in `allowed_backends` after smoke testing.

New adapter:

- `adapters/grok_cli.py`
  - Follows the CLI subprocess shape of `claude_cli.py` / `gemini_cli.py`.
  - Uses command shape:

    ```bash
    grok --no-auto-update --no-alt-screen \
      --cwd <access_root> \
      -m <model> \
      --output-format streaming-json \
      -p <prompt>
    ```

  - Parses newline JSON events.
  - Emits:
    - `KIND_TEXT_DELTA` for assistant chunks.
    - `KIND_THINKING` for reasoning/thought events if exposed.
    - `KIND_TOOL_START`, `KIND_TOOL_END`, `KIND_FILE_EDIT`, `KIND_SHELL_EXEC`, and `KIND_PROGRESS` where event fields support it.
  - Accumulates final visible text into `BackendResponse.text`.
  - Captures stream metadata for diagnostics.
  - Does not treat zero-exit empty text as success.

Tests:

- `tests/test_grok_cli_adapter.py`
  - version check success/failure.
  - parses `streaming-json` assistant text chunks into `KIND_TEXT_DELTA`.
  - reconstructs final response text.
  - handles stderr and nonzero exit.
  - treats zero-exit empty answer as failure.
  - retries once for `thought_end_no_text`.
  - skips retry for `side_effect_events_no_text`.
- `tests/test_answer_stream_capabilities.py`
  - assert `grok-cli` advertises `supports_answer_stream` only when parser path is enabled.
- registry/model tests:
  - `get_available_models("grok-cli")` includes `grok-composer-2.5-fast` and `grok-build`.
  - `is_cli_backend("grok-cli")` is true.

Docs:

- `docs/API_GUIDE.md`
- `docs/tools.md`
- `docs/AGENT_FYI.md`
- `docs/HASHI_REAL_STREAMING_OUTPUT_PLAN.md`

---

## 5. Implementation Phases And Current State

### Phase 0: Capture Real Event Shape ✅

Do this before writing production parser logic.

Commands:

```bash
grok --version
grok --no-auto-update --no-alt-screen \
  --cwd /tmp/hashi-grok-smoke \
  --output-format streaming-json \
  -p "Reply exactly: OK"
```

Save raw stdout/stderr to a local ignored fixture file, then distill a sanitized fixture into tests.

Acceptance:

- We know the exact `streaming-json` event schema.
- We know whether assistant deltas are emitted before final completion.
- We know the nonzero auth/error format.

### Phase 1: Minimal `grok-cli` Adapter ✅

Tasks:

- Add `GrokCLIAdapter`.
- Add registry/config/preflight wiring.
- Implement `generate_response()` with `streaming-json`.
- Emit `KIND_TEXT_DELTA` for answer chunks.
- Return accumulated final text.
- Do not enable in any live agent by default.

Acceptance:

- Unit tests pass from sanitized fixtures.
- `supports_answer_stream=True` only if the fixture proves real answer chunks.

### Phase 2: HASHI Runtime Integration ✅

Tasks:

- Add `grok-cli` to backend picker and `/backend` model selection.
- Add API gateway model exposure if desired.
- Add docs and operator commands.
- Add `answer_stream_final_delivery=true` live test path.

Acceptance:

- `temp` or a dedicated test agent can switch to `grok-cli`.
- Telegram final answer visibly streams when Grok emits text chunks.

### Phase 3: Live Smoke ✅

Preconditions:

- `grok` installed.
- Browser/CLI login has been completed for the `grok` command in the runtime environment.
- Use a non-critical test agent, not Zelda.

Smoke:

```text
/backend grok-cli model=grok-composer-2.5-fast
Write a concise but multi-paragraph explanation of this repository.
```

Acceptance:

- Placeholder grows with real answer text.
- Final delivery promotes or completes without duplicate final message.
- Logs show:

  ```text
  supports_answer_stream=True
  KIND_TEXT_DELTA count > 0
  Answer stream finalized ... promoted=True
  ```

### Phase 4: Optional ACP Transport

Deferred. Implement only if headless `streaming-json` becomes unstable enough to justify a second transport.

Tasks:

- Add an ACP transport mode inside `GrokCLIAdapter`.
- Launch `grok agent stdio`.
- Parse ACP `session/update` messages.
- Map `agent_message_chunk` to `KIND_TEXT_DELTA`.

Acceptance:

- Same user-visible streaming behavior as the headless CLI path.
- Authentication still relies on the official Grok CLI login state.

---

## 6. Security And Operational Notes

- Do not pipe-install Grok Build during automated tests.
- Treat `curl | bash` as a manual operator step only.
- Do not require `XAI_API_KEY` for the normal `grok-cli` backend. Treat auth like Codex/Claude: the CLI must already be logged in.
- Use `--no-auto-update` for scripted/headless HASHI calls to avoid surprise CLI updates mid-turn.
- Use `--no-alt-screen` so subprocess output remains machine-readable.
- Start with a test agent because Grok Build is early beta.
- If Grok Build performs its own file edits/tools, set HASHI `access_scope` conservatively and use a dedicated workspace for smoke tests.

---

## 7. Residual Risks And Open Questions

1. Live retry recovery remains unproven because the post-reboot live probe did not naturally trigger an empty-answer retry.
2. Grok CLI event schema may change while Grok Build is early beta. Keep parser tests current when upgrading the CLI.
3. Does `grok -p` support prompt via stdin for long prompts?
4. Does `--always-approve` bypass all tool confirmations, and should HASHI ever use it by default? Current HASHI usage should stay conservative.
5. Does Grok Build maintain per-directory session state that should map to HASHI `/new`, `/clear`, `/retry`, and handoff semantics?
6. Does xAI account access differ between browser login and headless WSL sessions?

---

## 8. Push Readiness Notes

The Grok support line is ready to push with the related hardening and audit commits:

- `0b53b2a Add basic Grok CLI backend`
- `c76bf1b Align Grok CLI default model`
- `1cb6980 Update Grok CLI streaming schema`
- `2736b9f Handle empty Grok CLI answers`
- `6b1aaf4 Harden Grok empty answer retry`

Related support-ecosystem hardening now also includes structured slash-command audit logging:

- `01da7cc Add structured slash command audit logging`
- `e359849 Complete slash command audit Phase 2 residual paths`
- `437f7e3 Fix slash dispatch policy bypass and bot suffix parsing`

Before pushing, run:

```bash
python3 -m py_compile adapters/grok_cli.py tests/test_grok_cli_adapter.py \
  orchestrator/slash_command_audit.py orchestrator/admin_local_testing.py \
  orchestrator/flexible_agent_runtime.py orchestrator/runtime_command_binding.py \
  orchestrator/workbench_api.py transports/whatsapp.py tests/test_slash_command_audit.py

pytest -q tests/test_grok_cli_adapter.py tests/test_answer_stream_capabilities.py \
  tests/test_model_catalog.py tests/test_slash_command_audit.py \
  tests/test_command_registry.py tests/test_runtime_command_binding.py \
  tests/test_queue_command.py tests/test_api_restart_commands.py \
  tests/test_wrapper_commands.py
```
