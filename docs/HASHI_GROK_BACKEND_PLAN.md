# HASHI Grok Backend Plan

**Status:** research and implementation plan.

**Date:** 2026-06-14.

**Goal:** add xAI Grok Build as a first-class HASHI backend, using the official authenticated CLI path with true final-output streaming where the CLI provides text deltas.

**Non-goal:** install Grok Build, authenticate to xAI, or change live agent routing before a human approves a smoke test.

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
- Current model docs list:
  - `grok-build-0.1`: fast coding model, 256k context, trained for agentic coding workflows.
  - `grok-4.3`: general model with 1M context and configurable reasoning.

Sources:

- https://x.ai/cli
- https://x.ai/news/grok-build-cli
- https://docs.x.ai/build/overview
- https://docs.x.ai/build/cli/headless-scripting
- https://docs.x.ai/developers/models

---

## 2. Recommended Architecture

HASHI should support Grok as a CLI-authenticated backend only:

- Engine name: `grok-cli`.
- Uses the official `grok` command in headless mode.
- Authentication is out-of-band, like `codex-cli` and `claude-cli`.
- Parses `--output-format streaming-json` and emits `KIND_TEXT_DELTA` for final answer chunks.
- ACP (`grok agent stdio`) can be considered later as an alternate CLI transport, not as an API fallback.

---

## 3. Backend Capability Target

Initial `grok-cli` capability should be:

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

The `supports_answer_stream=True` claim must be gated by a live fixture or smoke proving that `streaming-json` contains assistant text deltas. Until the event shape is captured, implement the parser behind tests and keep the docs honest.

If `streaming-json` proves unreliable, the only planned fallback is still CLI-based:

- Use ACP `grok agent stdio`, parse `session/update` with `agent_message_chunk`.

---

## 4. Files To Change

Core backend registration:

- `adapters/registry.py`
  - Add `grok-cli -> GrokCLIAdapter`.
- `orchestrator/flexible_backend_registry.py`
  - Add `grok-cli` to `CLI_ENGINES`.
  - Add registry entry:

    ```python
    "grok-cli": {
        "label": "grok",
        "models": ["grok-build-0.1", "grok-4.3"],
        "default_model": "grok-build-0.1",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [],
    }
    ```

- `orchestrator/config.py`
  - Add `grok_cmd: str = "grok"` to `GlobalConfig`.
  - Load `global.grok_cmd` from `agents.json`.
- `orchestrator/backend_preflight.py`
  - Include `grok-cli` command availability checks.
- `orchestrator/model_catalog.py`
  - Optional later: expose Grok models through the OpenAI-compatible local API gateway if CLI-backed gateway routing is desired.
- `agents.json`
  - Add `grok-cli` to selected agents' `allowed_backends` only after smoke testing.

New adapter:

- `adapters/grok_cli.py`
  - Follow the CLI subprocess shape of `claude_cli.py` / `gemini_cli.py`.
  - Prefer command:

    ```bash
    grok --no-auto-update --no-alt-screen \
      --cwd <access_root> \
      -m <model> \
      --output-format streaming-json \
      -p <prompt>
    ```

  - Use stdin transport for long or multiline prompts if the CLI supports it; otherwise keep prompt arg within safe limits.
  - Parse newline JSON events.
  - Emit:
    - `KIND_TEXT_DELTA` for assistant chunks.
    - `KIND_THINKING` for reasoning/thought events if exposed.
    - `KIND_TOOL_START`, `KIND_TOOL_END`, `KIND_FILE_EDIT`, `KIND_SHELL_EXEC`, and `KIND_PROGRESS` where event fields support it.
  - Accumulate final visible text into `BackendResponse.text`.
  - Capture usage/cost if exposed.

Tests:

- `tests/test_grok_cli_adapter.py`
  - version check success/failure.
  - parses `streaming-json` assistant text chunks into `KIND_TEXT_DELTA`.
  - reconstructs final response text.
  - handles stderr and nonzero exit.
  - supports no-delta fallback if CLI only emits final JSON.
- `tests/test_answer_stream_capabilities.py`
  - assert `grok-cli` advertises `supports_answer_stream` only when parser path is enabled.
- registry/model tests:
  - `get_available_models("grok-cli")` includes `grok-build-0.1`.
  - `is_cli_backend("grok-cli")` is true.

Docs:

- `docs/API_GUIDE.md`
- `docs/tools.md`
- `docs/AGENT_FYI.md`
- `docs/HASHI_REAL_STREAMING_OUTPUT_PLAN.md`

---

## 5. Implementation Phases

### Phase 0: Capture Real Event Shape

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

### Phase 1: Minimal `grok-cli` Adapter

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

### Phase 2: HASHI Runtime Integration

Tasks:

- Add `grok-cli` to backend picker and `/backend` model selection.
- Add API gateway model exposure if desired.
- Add docs and operator commands.
- Add `answer_stream_final_delivery=true` live test path.

Acceptance:

- `temp` or a dedicated test agent can switch to `grok-cli`.
- Telegram final answer visibly streams when Grok emits text chunks.

### Phase 3: Live Smoke

Preconditions:

- `grok` installed.
- Browser/CLI login has been completed for the `grok` command in the runtime environment.
- Use a non-critical test agent, not Zelda.

Smoke:

```text
/backend grok-cli model=grok-build-0.1
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

Implement only if headless `streaming-json` is not stable enough.

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

## 7. Open Questions

1. What is the exact `streaming-json` schema for:
   - assistant text chunks
   - final message
   - tool calls
   - file edits
   - errors
   - usage/cost
2. Does `grok -p` support prompt via stdin for long prompts?
3. Does `--always-approve` bypass all tool confirmations, and should HASHI ever use it by default?
4. Does Grok Build maintain per-directory session state that should map to HASHI `/new`, `/clear`, `/retry`, and handoff semantics?
5. Does xAI account access differ between browser login and headless WSL sessions?

---

## 8. Recommended First Patch

First patch should be a no-live-risk scaffold:

1. Add `grok_cmd` config.
2. Add `grok-cli` registry entry and preflight detection.
3. Add `GrokCLIAdapter` with fixture-driven parser tests.
4. Add docs.
5. Do not add `grok-cli` to active production agents yet.

Second patch should run live smoke and then add `grok-cli` to selected agents' `allowed_backends`.
