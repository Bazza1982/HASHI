# Claw-Code Optional Module Plan

Status: research plan only
Owner: HASHI
Last researched: 2026-05-20
Local research copy: `/home/lily/projects/external/claw-code`
Claw commit reviewed: `f8e1bb7262b261da1ee6bfcd461bfc5b676f6a6d`

## Decision

Use `claw-code` as an optional long-running coding-agent function/backend, not as
HASHI core and not as an embedded Rust library.

The first integration should be a subprocess boundary:

```text
HASHI runtime -> claw module/adapter -> claw CLI process -> .claw session state
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
- Local machine currently lacks `cargo`, so build/test must be a Phase 0 gate
  before any HASHI runtime integration is accepted.

## Proposed HASHI Shape

Add a module, not core code:

```text
modules/claw_code/
  README.md
  install_probe.py
  runner.py
  schemas.py
  tests/

adapters/claw_cli.py
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

This should be opt-in per agent. It should not change normal
`claude-cli`, `codex-cli`, `openrouter-api`, or `deepseek-api` behavior.

## Phase 0: External Spike

Goal: prove Claw works locally before touching HASHI runtime behavior.

Tasks:

1. Install Rust/Cargo on the host or choose a container path.
2. Build Claw from the external copy:

   ```bash
   cd /home/lily/projects/external/claw-code/rust
   cargo build --workspace
   ```

3. Run no-credential commands:

   ```bash
   ./target/debug/claw --help
   ./target/debug/claw status --output-format json
   ./target/debug/claw acp --output-format json
   ```

4. Run provider smoke with OpenRouter:

   ```bash
   OPENAI_BASE_URL=https://openrouter.ai/api/v1 \
   OPENAI_API_KEY=... \
   ./target/debug/claw --model openai/deepseek-v4-pro \
     --output-format json \
     prompt "reply with ready"
   ```

5. Run a workspace smoke in a disposable repo:

   ```bash
   claw init --output-format json
   claw --permission-mode read-only --output-format json prompt "summarize files"
   claw state --output-format json
   claw --resume latest /status
   ```

Acceptance:

- Build succeeds.
- JSON diagnostics parse.
- OpenRouter/DeepSeek route works.
- `.claw/worker-state.json` appears after a prompt.
- `.claw/sessions/...` appears and can resume.

## Phase 1: Read-Only HASHI Function

Goal: add a callable HASHI function that runs Claw for diagnostics only.

Add `modules/claw_code/runner.py` with:

- `find_claw_binary()`
- `run_claw_doctor(cwd, env) -> dict`
- `run_claw_status(cwd, env) -> dict`
- `run_claw_state(cwd, env) -> dict`

Rules:

- Never use shell string interpolation; pass args as arrays.
- Redact API keys from env/logs.
- Capture stdout/stderr separately.
- Apply a timeout.
- Store audit logs under HASHI logs.

Acceptance:

- HASHI can call `claw doctor --output-format json`.
- HASHI can call `claw status --output-format json`.
- Missing binary, missing cargo build, and missing credentials produce typed
  errors.

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

Run Phase 0 only. If it passes, implement Phase 1 as a small optional module.

Do not add `claw-cli` to active agents until:

- build passes;
- no-credential diagnostics pass;
- OpenRouter/DeepSeek smoke passes;
- read-only workspace smoke passes;
- workspace-write disposable patch smoke passes.
