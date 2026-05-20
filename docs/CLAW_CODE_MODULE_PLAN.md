# Claw-Code Optional Module Plan

Status: research plan updated after local build/tool smoke
Owner: HASHI
Last researched: 2026-05-20
Local research copy: `/home/lily/projects/external/claw-code`
Claw commit reviewed: `f8e1bb7262b261da1ee6bfcd461bfc5b676f6a6d`
Local release binary tested:
`/home/lily/projects/external/claw-code/rust/target/release/claw`

## Decision

Use `claw-code` as an optional long-running coding-agent function/backend, not as
HASHI core and not as an embedded Rust library.

The first integration should be a subprocess boundary:

```text
HASHI runtime -> claw adapter -> configured claw CLI binary -> .claw session state
```

This keeps HASHI minimal and hot-rebootable while allowing Claw to own its own
agent loop, file tools, permission model, session persistence, compaction, and
provider routing.

## Why It Is Useful

Claw already contains several pieces HASHI would otherwise have to build for
API models:

- `ConversationRuntime` with model/tool iteration.
- Session persistence and resume via `.claw/sessions/...`.
- Workspace-bound session namespacing.
- Auto-compaction threshold support.
- Permission modes: `read-only`, `workspace-write`, `danger-full-access`.
- Tool specs and dispatch for bash, read/write/edit, grep/glob, web, tasks,
  MCP/LSP registry surfaces, plugin tools, and agent/subagent surfaces.
- OpenAI-compatible provider routing through `OPENAI_BASE_URL` and
  `OPENAI_API_KEY`, which can point to OpenRouter or local OpenAI-compatible
  servers.
- Machine-readable JSON output for diagnostic commands.

This maps well to the goal of releasing more capability from DeepSeek and other
API models without making HASHI's native API backend loop overly complex.

## Important Limitations

- There is no real ACP/Zed daemon or JSON-RPC service today. `claw acp serve`
  is explicitly a status alias and does not start a socket.
- Integration should not depend on hidden daemon behavior.
- The stable external surface today is CLI + files:
  - `claw doctor --output-format json`
  - `claw status --output-format json`
  - `claw state --output-format json`
  - `claw prompt ...`
  - `claw --resume latest ...`
  - `.claw/worker-state.json`
  - `.claw/sessions/...`
- Local Linux/WSL build/test now passes, but build and failure-path tests must
  remain Phase 0 gates before any HASHI runtime integration is accepted.

## Local Smoke Evidence

On 2026-05-20, the external research copy was tested locally:

- `cargo build --workspace` succeeded.
- `cargo build --release -p rusty-claude-cli` succeeded.
- Release binary:
  `/home/lily/projects/external/claw-code/rust/target/release/claw`
- Release binary size: about 17 MB.
- Release binary SHA256:
  `910be0eaef337a2ad3fb22a761bbf72cff572ca3f5e52891b53ad5574a8f697e`
- `claw version --output-format json` returned `version=0.1.0`,
  `git_sha=f8e1bb7`, and `target=x86_64-unknown-linux-gnu`.
- `claw status --output-format json`, `claw config --output-format json`, and
  `claw doctor --output-format json` returned parseable JSON.
- OpenRouter route with model `deepseek/deepseek-v4-flash` returned
  `message=ready`.
- A read-only tool smoke with `--allowedTools read` successfully used
  `read_file` on a disposable workspace and returned the expected answer.

This proves the Linux/WSL local path is viable. It does not prove native
Windows/HASHI9 packaging or runtime integration.

## Proposed HASHI Shape

Do not add a new generic `modules/` layer for Claw. HASHI does not currently
have a module directory convention, and a one-off `modules/claw_code/` would
create a new architecture category without enough design justification.

Use the existing adapter shape first:

```text
adapters/claw_cli.py
scripts/claw_code_probe.py
tests/test_claw_cli_adapter.py
```

The adapter exposes Claw to HASHI as a backend-like function:

```json
{
  "engine": "claw-cli",
  "model": "openai/deepseek-v4-pro",
  "permission_mode": "workspace-write",
  "enabled": false
}
```

Runtime must accept a configured binary path, for example:

```json
{
  "claw_binary_path": "/home/lily/projects/external/claw-code/rust/target/release/claw"
}
```

This should be opt-in per agent. It should not change normal
`claude-cli`, `codex-cli`, `openrouter-api`, or `deepseek-api` behavior.

## Phase 0: External Spike

Goal: prove Claw works locally before touching HASHI runtime behavior.

Tasks:

1. Prefer a prebuilt upstream release binary when one exists. Download its
   matching checksum and verify it before use.
2. If no release binary exists, build from source as a research-only fallback.
   Cargo is allowed for this spike, but must not become a HASHI runtime
   dependency.
3. Build Claw from the external copy if using the fallback path:

   ```bash
   cd /home/lily/projects/external/claw-code/rust
   cargo build --release -p rusty-claude-cli
   ```

4. Run no-credential commands:

   ```bash
   ./target/release/claw --help
   ./target/release/claw status --output-format json
   ./target/release/claw doctor --output-format json
   ```

5. Run provider smoke with OpenRouter:

   ```bash
   OPENAI_BASE_URL=https://openrouter.ai/api/v1 \
   OPENAI_API_KEY=... \
   ./target/release/claw --model deepseek/deepseek-v4-flash \
     --output-format json \
     prompt "reply with ready"
   ```

6. Run a workspace smoke in a disposable repo:

   ```bash
   claw init --output-format json
   claw --permission-mode read-only --output-format json prompt "summarize files"
   claw state --output-format json
   claw --resume latest /status
   ```

7. Run failure-path smoke tests:

   - Missing or invalid binary path.
   - Invalid API key.
   - Invalid model name.
   - Read-only `.claw/` directory.
   - Non-JSON or truncated command output.
   - Timeout and cancellation.

Acceptance:

- Build succeeds.
- JSON diagnostics parse.
- OpenRouter/DeepSeek route works.
- `.claw/worker-state.json` appears after a prompt.
- `.claw/sessions/...` appears and can resume.
- Failure modes produce clear non-zero exits or typed errors, not silent
  success.

## Phase 1: Read-Only HASHI Function

Goal: add a callable HASHI diagnostic path that runs Claw diagnostics only.

Add `scripts/claw_code_probe.py` and internal helpers in `adapters/claw_cli.py`
with:

- `find_claw_binary()`
- `run_claw_doctor(cwd, env) -> dict`
- `run_claw_status(cwd, env) -> dict`
- `run_claw_state(cwd, env) -> dict`
- `ClawBinaryNotFound`
- `ClawCommandError`
- `ClawJsonError`
- `ClawTimeoutError`

Rules:

- Never use shell string interpolation; pass args as arrays.
- Redact API keys from env/logs.
- Capture stdout/stderr separately.
- Apply a timeout.
- Store audit logs under HASHI logs.
- Do not require Cargo at runtime.
- If `claw_binary_path` is missing or invalid, report a typed error.

Acceptance:

- HASHI can call `claw doctor --output-format json`.
- HASHI can call `claw status --output-format json`.
- Missing binary, missing configured build artifact, and missing credentials
  produce typed errors.
- A missing binary does not prevent unrelated agents from starting.

## Phase 2: One-Shot Claw Task Function

Goal: run one Claw prompt as a HASHI function.

Proposed interface:

```python
run_claw_task(
    workspace_dir: Path,
    prompt: str,
    model: str,
    permission_mode: str = "workspace-write",
    resume: str | None = None,
    timeout_s: int = 1800,
) -> ClawTaskResult
```

Command shape:

```bash
claw \
  --model <model> \
  --permission-mode <mode> \
  --output-format json \
  prompt <prompt>
```

For continuation:

```bash
claw --resume latest --output-format json prompt <prompt>
```

Acceptance:

- Can run a read-only analysis task.
- Can run a workspace-write patch task in a disposable repo.
- HASHI captures result text, return code, stdout/stderr, elapsed time, and
  worker/session paths.
- Invalid API key, invalid model, read-only `.claw/`, timeout, and cancellation
  have explicit result states.

## Phase 3: Backend Adapter

Goal: expose Claw as `claw-cli` alongside existing CLI backends.

Add `adapters/claw_cli.py` with the same high-level contract as existing CLI
backends:

- initialize
- generate_response
- stream or pseudo-stream status events
- timeout/cancel
- structured error mapping

Use Claw's own session as the long-running state. HASHI owns queueing,
delivery, logging, and restart boundaries.

Lifecycle rules:

- `initialize()` returns `False` if the configured Claw binary is missing,
  not executable, or fails `version --output-format json`.
- A failed `initialize()` puts only that agent/backend into degraded state and
  must not block other agents.
- Do not silently fall back to `openrouter-api` or `deepseek-api`; the user
  must be able to see that Claw is unavailable.
- `handle_new_session()` creates or switches to a new Claw session reference.
  It must not delete old `.claw/sessions` files by default.
- Adding `claw-cli` to `adapters/registry.py` requires one cold restart, just
  like any new adapter class. Later config changes can follow normal HASHI
  reboot behavior.
- If another live Claw process owns the same workzone, the adapter must refuse
  to start the task and report a clear conflict. Use a lock file or the
  `.claw/worker-state.json` pid when available.

Acceptance:

- Agent can switch to `claw-cli`.
- A task can resume after HASHI restart using `--resume latest`.
- HASHI logs include Claw command, cwd, model, permission mode, return code,
  duration, output lengths, and session state path.

## Phase 4: Long-Running Task Supervision

Goal: make it useful for real long coding work.

Add:

- periodic state polling via `claw state --output-format json`;
- terminal/progress events derived from Claw JSON and `.claw/worker-state.json`;
- cancellation by process group;
- timeout tiers for short/medium/long tasks;
- artifact collection from `.claw/sessions` and `.claw/worker-state.json`;
- final diff/test summary collection.

Acceptance:

- HASHI can show a long task as running, blocked, failed, or complete.
- Cancelling from HASHI stops the Claw subprocess.
- Crash/reboot can report previous Claw session state.

## Security Rules

- Do not pass HASHI secrets wholesale. Build a minimal environment per task.
- The Claw subprocess environment should be an allowlist, not a copy of
  `os.environ`.
- Initial allowlist:
  - `OPENAI_BASE_URL`
  - `OPENAI_API_KEY`
  - OS-required process variables such as `HOME`, `USER`, `TMPDIR`, `TEMP`, and
    a minimal `PATH` only when needed to execute the configured binary.
- Do not pass Anthropic keys, HASHI instance secrets, WhatsApp/Bridge tokens,
  or unrelated agent secrets to Claw.
- Redact `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and any
  key-like values from logs.
- Default to `workspace-write`, not `danger-full-access`.
- Use `read-only` for review/research tasks.
- Keep Claw's `.claw/` state inside the target workzone, not HASHI core.
- Never let Claw operate in `/home/lily/projects/hashi` unless the user
  explicitly asks for HASHI code work.

## Fit Assessment

Good fit:

- long coding tasks;
- patch/test/review loops;
- DeepSeek or OpenRouter models needing a stronger agent harness;
- disposable repo tasks where Claw can own `.claw` state.

Poor fit:

- replacing HASHI core;
- real-time chat responses;
- lightweight file lookup;
- Watchtower/remote status;
- browser/desktop orchestration.

## Recommended Next Step

Phase 0 Linux/WSL smoke has passed for local build, OpenRouter/DeepSeek, and
read-only file tool use. Before runtime integration, update this plan into an
execution checklist and run the remaining failure-path tests.

Then implement Phase 1 as a small optional adapter/probe path, not a new
generic module layer.

Do not add `claw-cli` to active agents until:

- build passes;
- no-credential diagnostics pass;
- OpenRouter/DeepSeek smoke passes;
- read-only workspace smoke passes;
- workspace-write disposable patch smoke passes.
- failure-path smokes pass;
- binary missing/degraded lifecycle behavior is implemented;
- subprocess environment allowlist is implemented.
