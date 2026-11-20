# Local agent handover: HLI end-to-end validation (not production)

**Authorisation:** work only in fresh worktrees from both HLI feature branches;
test and fix on those branches, then push to the same Draft PRs for review.
Do **not** edit or restart running HASHI/Workbench/Compute production,
do not touch protected HASHI Core, do not merge, and do not enable GitHub CI.
This change adds a keyless inference provider and must not introduce new
GPU locks, quotas, model whitelists, artificial capacity rejections, forced
timeouts or duplicate authorisation gates. Keep existing explicit access
controls unchanged.

## Repositories

1. `Bazza1982/hashi_compute`: `feat/hli-local-inference-20261119`
2. `Bazza1982/HASHI`: `feat/hli-local-inference-20261119`

Read `hashi_compute/docs/HLI_LOCAL_INFERENCE.md` and `HASHI/docs/HLI_PROVIDER.md`.
Read the actual changed code, not only the PR summaries, before testing.

## Known workstation and safe staging

Windows with AMD 5950X, 64 GB RAM, RTX 3090 24 GB and RTX 5070 Ti 16 GB;
1000 W PSU and tight dual-card airflow. Existing Hashi Compute has Ollama
and SOK creative services; both must remain functional. HASHI local path has
previously been `C:\Users\thene\projects\HASHI4`; SOK at
`D:\Projects\content_generation`. Discover the **actual** git roots and
configured Compute port on this machine; do not assume those paths or
ports represent a staging instance. Use fresh directories and *free* ports.
Suggested: staging HLI host 13100, llama worker 13110+, staging Compute 19546.

1. `git fetch origin feat/hli-local-inference-20261119` in **each** repo;
   make independent worktrees under new paths (no checkout of production).
2. Use your existing Compute Python environment. Build/download a **recent,
   verified Windows CUDA-enabled llama-server**, supporting both Ampere
   and Blackwell drivers on this machine. Do not replace the running Ollama
   binary or driver. Verify `llama-server --help` for `--jinja`, `--parallel`,
   `--ctx-size` and split-mode flags. If installed binary differs, adapt HLI
   argv to the installed version in the PR, not via user-facing restrictions.
3. Copy `hli.example.json` into local ignored config; provide valid GGUF
   paths, unique model IDs, per-slot context, parallel count, replicas and
   placement. Begin with one already-local small GGUF. Avoid needless
   downloads; only GGUF files may be passed to llama-server.
4. Start `python -m hashi_compute.hli_host --config <staging-hli-json>` in
   staging terminal. Confirm `/health/ready`, `/v1/models`, native SSE and
   tool-call JSON. No API key, no GPU capacity preapproval, no password prompt.
5. In a **separate staging Compute state/config**, enable a service kind
   `hli` pointing at the staging HLI host. `models: []` means all its models.
   Confirm Compute `/v1/models`, `/v1/model-capabilities`, `/v1/manifest`
   and streaming `/v1/chat/completions`, with preserved model IDs/capacity.
6. In a **separate staging HASHI agent** add `hli-api` with an explicit
   model list and staging Compute URL, then select it via HERV3. No API key.
   Test model selection, switching and all reasoning efforts; if template
   lacks a certain effort level, degrade gracefully without claiming support.
7. Confirm the HERV3 small-model tool set (including on-demand `tool_find`
   and Office/developer tools when already installed) is available by default,
   and a real tool call is executed exactly once. Verify tool-result
   continuation and JSON arguments, `/stop`, SSE disconnect and recovery.
8. Exercise HLI automatic Compact with 8K/16K contexts; assert measured
   prompt never silently exceeds the native per-slot context. Test switching
   a long history to smaller model; report any existing preflight restriction
   that should be replaced by Compact, do not bypass actual context limits.

## GPU acceptance scenarios (actual evidence required)

- Independently run one small model on 3090, then 5070 Ti (UUID-based).
- Run two distinct small models on separate GPUs concurrently, no queues,
  despite each individually fitting on one card.
- Run `replicas: 2` of the same model with concurrent requests on both GPUs;
  compare single replica / 1, 2, 4 slots throughput and first-token latency.
- Run a model that cannot fit on one card in `placement: "all"`/`"split"`:
  verify true cross-GPU weights residency via `nvidia-smi`; compare vs
  single-GPU + CPU offload, and tune native split if useful.
- Benchmark against **existing Ollama** using same GGUF/quant/context and
  consistent prompts, 4K context, quality and tool success, not just tok/s.
- In mixed HLI + Ollama + SOK image/video/STT/TTS workloads, verify no
  pre-existing service regressions, GPU OOM recovery or resource conflicts.
- For failure injection: invalid GGUF path, missing GPU, process crash,
  client disconnect, stale CUDA UUID, and model swap during active generation.

Log both GPU UUIDs, GPU memory/utilisation, llama-server flags, model GGUF
size/quantisation, context, slots, model throughput total and per request,
TTFT, errors, maximum concurrent jobs and cancellation latency. Do not
introduce an unrequested admission scheduler to make tests pass. Record
where actual hardware VRAM or thermal limits constrain throughput.

## Test commands and handoff

Compute: `python -m unittest discover -s tests -p test_hli_host.py -v`

HASHI: `python -m unittest discover -s tests -p test_hli_provider.py -v`

Run targeted existing regression tests if installed (use existing pytest
environment where available), plus non-production end-to-end acceptance.
Add **only** directly relevant regression tests for observed failures; do not
trigger GitHub Actions. Push fixes to the same two feature branches and
report each PR URL/SHA, passes/failures and unresolved issues. Leave both PRs
Draft and both production instances untouched for my review.

## Small-model context envelope: measured, not fixed by these PRs

Measured 2026-11-20 on DESKTOP-VN0AMD7 with a minimal `hli8k` agent and a
fresh session (calibrated 1.37 native tokens/character):

- tool definitions: 13,032 chars (10 default core tool schemas)
- tools_catalogue PCM block: 14,413 chars
- skills_catalogue 1,820 + message_context 1,652 + rest ~740 chars
- system contracts (v3 MAIN/QUESTION + persona + /sys): ~8,500 chars
- total fixed envelope: >= ~40,000 chars == ~55,000-63,000 native tokens

An 8K native slot (8192 tokens minus 2048 response headroom = 6144 usable
input) therefore cannot accept a full HERV3 turn before any history or tool
results are added. Automatic Compact reduces the managed-history section and
the tool schemas, but it cannot shrink this fixed envelope below the slot,
so these PRs do NOT claim that Compact makes an 8K turn workable.

What these PRs do keep:

- proactive Compact that rebuilds the payload actually sent;
- typed `CONTEXT_CAPACITY_REJECTED` mapping for native overflow;
- no new estimator-based hard rejection.

Making a real 8K turn work requires a Core-level compact envelope (a small
fixed system/tool-catalogue projection), which touches protected Core and is
therefore a separate design/review item with its own approval. It is
intentionally NOT part of these two PRs.
