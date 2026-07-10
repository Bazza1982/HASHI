# HASHI xAI API Backend (Hermes OAuth)

**Status:** Phase 3 implemented (responses API, Imagine hook, secrets OAuth refresh).

**Date:** 2026-07-07.

**Goal:** expose the full xAI model surface (`grok-4.5`, `grok-4.3`, `grok-build-0.1`, Imagine, and related
chat models) through HASHI as a native HTTP backend (`xai-api`), using SuperGrok subscription
OAuth credentials managed by Hermes, with automatic token refresh.

**Non-goal:** replace `grok-cli`. The CLI backend remains the coding-agent path with local
tool/shell side effects. `xai-api` is the stateless HTTP completion path for API Gateway,
flex agents, and remote callers that need the broader xAI catalog.

**Related:** [HASHI_GROK_BACKEND_PLAN.md](HASHI_GROK_BACKEND_PLAN.md) (`grok-cli`, implemented).

---

## 0. Why A Separate Backend

HASHI already ships `grok-cli` for agent runtime use. Two gaps remain:

| Gap | `grok-cli` today | `xai-api` target |
| --- | --- | --- |
| API Gateway (`18801`) | not in model catalog | OpenAI-compatible routing |
| Model breadth | `grok-composer-2.5-fast`, `grok-build` | `grok-4.5`, `grok-4.3`, `grok-4.20-*`, `grok-build-0.1`, Imagine, … |
| Transport | subprocess → Grok CLI | `httpx` → `https://api.x.ai/v1` |
| Auth | CLI browser login (`~/.grok/auth.json`) | Hermes OAuth with auto-refresh |
| Side effects | real shell/file tools via CLI | none unless HASHI `ToolRegistry` is attached |

Keep both engines. They solve different problems.

---

## 1. Validated Auth Method (2026-07-07)

Hermes xAI OAuth was tested on this machine. Summary:

| Method | Result |
| --- | --- |
| `hermes chat --provider xai-oauth --model grok-4.3` | ✅ works |
| Read `access_token` from `auth.json` and POST directly | ❌ `403 unauthenticated:bad-credentials` |
| `resolve_xai_http_credentials(force_refresh=True)` then POST | ✅ `200` on `/v1/chat/completions` and `/v1/responses` |

**Conclusion:** OAuth is viable. HASHI must **not** treat the on-disk `access_token` as a static
API key. The adapter needs the same refresh semantics Hermes uses at runtime.

### Corrected auth flow

```
1. Prefer Hermes' own xAI OAuth resolver when Hermes is installed locally
2. Let Hermes handle credential_pool selection, refresh locking, and rotated refresh_token persistence
3. Fallback to credential_pool access_token only when the Hermes resolver cannot be imported
4. Fallback to standalone secrets.json refresh/API key only when no Hermes-managed profile is configured
4. Call https://api.x.ai/v1 with Authorization: Bearer <fresh_access_token>
5. On 401: force Hermes resolver refresh once and retry exactly once
```

### Hermes auth store (this machine)

| Item | Value |
| --- | --- |
| Hermes home | `C:\Users\thene\AppData\Local\hermes` (via `get_hermes_home()`) |
| Auth file | `auth.json` under Hermes home — **not** `C:\Users\<user>\.hermes\auth.json` |
| Schema | v1: `providers.xai-oauth` + `credential_pool["xai-oauth"][]` |
| Runtime source | Hermes resolver over `credential_pool["xai-oauth"][]` / `providers.xai-oauth.tokens` |
| Base URL | `https://api.x.ai/v1` |

WSL path equivalent: `/mnt/c/Users/thene/AppData/Local/hermes/auth.json`.

### What not to do

- Do not copy the simplified guide shape `auth.json["xai-oauth"]["access_token"]` — stale on disk.
- Do not skip refresh because Hermes chat succeeded earlier; chat refreshes in-process.
- Do not spend Hermes refresh tokens directly from HASHI as the primary path. xAI refresh tokens can
  rotate; direct refresh without Hermes' lock/write-through contract can leave other Hermes profiles
  holding a revoked token.
- Do not point HASHI at the global Hermes root when the active xAI OAuth credential lives in a profile
  such as `profiles/xiaoye`.

---

## 2. Architecture

```text
Client (OpenAI SDK / Telegram / Remote)
    → API Gateway :18801  OR  Flex Agent runtime
    → engine map: model → xai-api
    → XaiApiAdapter (new)
    → XaiOAuthCredentialResolver
         ├─ import Hermes resolver when available
         ├─ use Hermes credential_pool access token as fallback
         ├─ standalone secrets.json OAuth refresh (headless fallback)
         └─ secrets.json xai_api_key (optional)
    → POST https://api.x.ai/v1/chat/completions
       or POST https://api.x.ai/v1/responses
    → OpenAI-shaped HASHI BackendResponse / stream events
```

### Engine identity

- Engine ID: `xai-api`
- Adapter: `adapters/xai_api.py` (proposed; follow `adapters/deepseek_api.py` pattern)
- Registry: `adapters/registry.py`, `orchestrator/flexible_backend_registry.py`
- Secret keys (lookup order): `xai_oauth_refresh_token`, `xai_api_key`, `XAI_API_KEY`
- Hermes auth path override (optional `agents.json` global): `hermes_home`

### Coexistence with `grok-cli`

| Engine | Use when |
| --- | --- |
| `grok-cli` | Coding agent turns, local tools, Grok Build subprocess semantics |
| `xai-api` | Gateway clients, broader chat models, stateless HTTP, Imagine/TTS later |

Model IDs are **not** interchangeable across engines (`grok-build` ≠ `grok-build-0.1`).

---

## 3. Credential Resolver Design

New module: `adapters/xai_oauth.py` (or `orchestrator/xai_oauth_credentials.py`).

### Resolution order

1. **In-memory cache** — return if `expires_at - skew > now`
2. **Hermes resolver** — call `hermes_cli.auth.resolve_xai_oauth_runtime_credentials()`
3. **Hermes auth.json fallback** — use a current `credential_pool` access token if resolver import is unavailable
4. **secrets.json `xai_oauth_refresh_token`** — standalone refresh for headless hosts without Hermes
5. **secrets.json `xai_api_key`** — static console key fallback (no refresh)
4. Fail preflight with actionable message: `hermes auth add xai-oauth --type oauth`

### Refresh implementation options

| Option | Complexity | Recommendation |
| --- | --- | --- |
| A. Import Hermes resolver from local `hermes-agent` | low | preferred for WSL/Windows co-host |
| B. Use credential_pool access token without refresh | low | fallback only; handles already-live Hermes profiles |
| C. Store only `xai_oauth_refresh_token` in `secrets.json` | medium | best for servers without Hermes UI |

Production should prefer **A**. Direct HASHI-owned refresh of Hermes tokens is only safe if HASHI also
persists rotated refresh tokens under the same auth-store contract.

### Concurrency

- One refresh lock per process (same pattern as Hermes `CredentialPool` threading lock).
- Multiple concurrent HASHI requests share the cached access token.

---

## 4. HTTP Adapter Design

Base: `OpenRouterAdapter` / `DeepSeekAdapter` (`httpx`, streaming, tool loop).

### Endpoints

| Phase | Endpoint | Notes |
| --- | --- | --- |
| 1 | `POST /v1/chat/completions` | maximum code reuse, OpenAI SDK compatible |
| 2 | `POST /v1/responses` | reasoning, prompt caching, Grok Build API models |

Default base URL: `https://api.x.ai/v1`.

### Initial model catalog

Current API Gateway text models (OAuth-verified):

- `grok-4.5` (default; Responses API, smoke-tested 2026-07-10)
- `grok-4.3`
- `grok-build-0.1`
- `grok-4.20-0309-reasoning`
- `grok-4.20-0309-non-reasoning`
- `grok-4.20-multi-agent-0309`

Deferred (separate tools or later phase):

- `grok-imagine-image`, `grok-imagine-image-quality`
- `grok-imagine-video`, `grok-imagine-video-1.5-preview`
- Voice / TTS / X Search

Optional later: dynamic `GET /v1/models` refresh into catalog cache.

### Capabilities

```python
BackendCapabilities(
    supports_sessions=False,
    supports_files=False,
    supports_tool_use=True,        # via ToolRegistry when configured
    supports_thinking_stream=True,
    supports_headless_mode=True,
)
capabilities.supports_answer_stream = True
```

Attach `ToolRegistry` in `FlexibleBackendManager` the same way as `openrouter-api`.

### Remote backend policy

Treat `xai-api` like other costed API backends:

- block automated scheduler / cron sources unless explicitly allowed
- allow user-initiated Telegram, Gateway, and approved remote traffic

---

## 5. API Gateway Integration

After `xai-api` adapter exists, extend gateway catalog (same pattern as Gemini/Claude/Codex):

| File | Change |
| --- | --- |
| `orchestrator/model_catalog.py` | `AVAILABLE_XAI_API_MODELS` |
| `orchestrator/api_gateway.py` | map models → `xai-api` in `_ENGINE_FOR_MODEL` |
| `orchestrator/api_gateway_config.py` | include in `available_api_models()` |
| `docs/API_GUIDE.md` | document xAI OAuth models |

Gateway adapter pool already calls `get_backend_class(engine)` — no pool rewrite required.

**Optional fast path:** wire existing `grok-cli` models into Gateway first (~50 LOC) while `xai-api`
is being built. That is independent and smaller in scope.

---

## 6. Configuration

### `agents.json` (global, proposed)

```json
{
  "hermes_home": "C:\\Users\\thene\\AppData\\Local\\hermes",
  "xai_api_base_url": "https://api.x.ai/v1",
  "xai_oauth_enabled": true
}
```

WSL deployments should set `hermes_home` to the `/mnt/c/...` path or leave unset and auto-detect
Windows Hermes home when present.

### `secrets.json` (optional overrides)

```json
{
  "xai_api_key": "",
  "xai_oauth_refresh_token": ""
}
```

Prefer Hermes-managed OAuth on developer machines. Use `xai_api_key` only when OAuth is unavailable.

### Flex agent example

```json
{
  "engine": "xai-api",
  "model": "grok-4.5"
}
```

---

## 7. Implementation Plan

### Phase 1 — Credential resolver + minimal adapter

- [x] `adapters/xai_oauth_credentials.py` — read Hermes auth, refresh, in-memory cache
- [x] `adapters/xai_api.py` — chat/completions, sync + stream
- [x] `adapters/registry.py` — register `xai-api`
- [x] `orchestrator/flexible_backend_registry.py` — models + secret keys
- [x] `orchestrator/backend_preflight.py` — Hermes auth or `xai_api_key` present
- [x] `orchestrator/api_gateway.py` + `api_gateway_config.py` — gateway model catalog
- [x] `tests/test_xai_api_adapter.py` + `tests/test_xai_oauth_credentials.py`

### Phase 2 — Gateway + ops

- [x] Gateway model catalog wiring
- [x] `docs/API_GUIDE.md` update
- [x] Preflight in gateway startup health (`orchestrator/api_gateway_preflight.py`)
- [x] Remote backend policy registration (`xai-api` in flex + legacy runtime)
- [x] `agents.json` `hermes_home` configured for HASHI1

### Phase 3 — Responses API + media

- [x] `/v1/responses` transport (`grok-build-*` models; optional `xai_use_responses_api`)
- [x] Imagine image hook (`grok-imagine-image*`, `xai_imagine` tool, `/images/generations`)
- [x] Standalone refresh without Hermes dependency (`secrets.json` `xai_oauth_refresh_token`)
- [x] Hermes-managed resolver path for WSL/Windows profile auth

---

## 8. Verification Checklist

### Auth

```bash
# Hermes path still healthy
hermes chat --provider xai-oauth --model grok-4.5 -q "Reply exactly: OK"
```

### HASHI adapter (after implementation)

```bash
pytest -q tests/test_xai_api_adapter.py

# Gateway
curl -s http://10.255.255.254:18801/v1/models | jq '.data[].id' | grep grok-4

curl -s -X POST http://10.255.255.254:18801/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Reply exactly: OK"}]}'
```

### Negative test

- Using stale `access_token` from disk without refresh must fail (regression guard).

---

## 9. Security And Operational Notes

- OAuth refresh tokens are secrets — never log token bodies, never commit `auth.json`.
- Prefer reading Hermes auth read-only; avoid corrupting Hermes credential pool rotation state.
- `xai-api` has no CLI sandbox — scope `ToolRegistry` conservatively when enabled.
- Document that SuperGrok OAuth and console `XAI_API_KEY` billing models differ.
- On `403 bad-credentials`, surface: re-run `hermes auth add xai-oauth --type oauth`.

---

## 10. Open Questions

1. Should HASHI auto-detect Hermes home on WSL, or require explicit `agents.json` path?
2. Do we write refreshed access tokens back to Hermes store, or keep HASHI cache only?
3. Should Gateway expose both `grok-cli` and `xai-api` models simultaneously, or `xai-api` only
   for public HTTP clients?
4. Is standalone OAuth refresh (without Hermes install) required for headless Linux servers?

---

## 11. References

- Hermes resolver: `tools/xai_http.py` → `resolve_xai_http_credentials()`
- Hermes OAuth runtime: `hermes_cli/auth.py` → `resolve_xai_oauth_runtime_credentials()`
- Hermes credential pool: `hermes_cli/proxy/adapters/xai.py`
- xAI docs: https://docs.x.ai/developers/quickstart
- xAI models: https://docs.x.ai/developers/models
- HASHI OpenAI Gateway: [API_GUIDE.md](API_GUIDE.md)
- HASHI CLI Grok backend: [HASHI_GROK_BACKEND_PLAN.md](HASHI_GROK_BACKEND_PLAN.md)
