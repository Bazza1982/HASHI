# Hashi Flex-Backend Upgrade Plan: "Omni-Brain" 🚀

**Date**: 2026-03-12  
**Target**: `flexible_agent_runtime.py`, `flexible_backend_manager.py`  
**Status**: Implemented on 2026-03-12

---

## 1. Executive Summary

Current "Flex" implementation allows seamless switching between CLI-based engines (Gemini, Claude, Codex). However, integrating **OpenRouter (API-based)** introduces gaps in model identifier formatting and credential management. This plan outlines a robust path to make OpenRouter a "first-class citizen" in the Flex architecture, ensuring zero-error switching and dynamic model awareness.

---

## 2. Identified Bottlenecks

### 2.1 Credential Rigidity
- **Current**: OpenRouter API keys are expected to be global (`openrouter-api_key`).
-   **Goal**: Allow per-agent keys to facilitate multi-account management or usage tracking.

### 2.2 The "Two-Stage" Switching Gap
- **Current**: `/backend` command switches the engine but leaves the old model identifier active.
-   **Risk**: Switching from `gemini-cli` (model: `gemini-2.5-flash`) to `openrouter-api` results in an invalid request because the API doesn't recognize the CLI-short-name.

### 2.3 Static Model Metadata
- **Current**: Model lists are hardcoded in `agent_runtime.py`.
-   **Risk**: OpenRouter adds/deprecates models daily; hardcoded lists become obsolete quickly.

---

## 3. Implementation Plan

### Phase 1: Tiered Credential Loading (`flexible_backend_manager.py`)
Modify the `initialize_active_backend` method to support a fallback key mechanism:
1.  **Check Agent-Specific Key**: `secrets.get(f"{agent_name}_openrouter_key")`
2.  **Fallback to Global Key**: `secrets.get("openrouter-api_key")` or `secrets.get("openrouter_key")`

### Phase 2: Atomic "Brain + Model" Switching UI (`flexible_agent_runtime.py`)
Refactor the `/backend` command and its callback handlers to eliminate the "unsupported model" state:
1.  **Persistent Keyboard**: When a user selects a backend via `/backend`, do **not** close the message or simply report success.
2.  **Chain Reaction**: Immediately update the inline keyboard to show the specific model list for the *selected* backend.
3.  **Delayed State Commit**: The `state.json` and active runtime backend are only updated once the specific model has been confirmed.
4.  **Model Mapper (`DEFAULT_MODEL_PER_BACKEND`)**: Add a dict mapping each backend to its sensible default model (e.g. `gemini-cli → gemini-2.5-flash`, `openrouter-api → anthropic/claude-sonnet-4`). When switching backends, if the current model is invalid for the target backend, auto-select the default. This eliminates the "unsupported model" state even without the chained keyboard (e.g. programmatic switches, `/backend +` handoff).
5.  **Preserve `/backend +` (context handoff)**: The new chained keyboard flow must preserve the `+` flag for continuity handoff. If the user invokes `/backend openrouter-api +`, the two-stage UI (pick backend → pick model) still applies, and the handoff context is built and injected after model confirmation — not after backend selection alone.

> **Note — Rollback already exists**: `flexible_backend_manager.py` (lines 74-102) already implements automatic rollback if the target backend fails to init. Phase 2 should leverage this, not rebuild it.

> **Note — `/model` command**: The existing `/model` command reads `self.config.active_backend` and calls `_get_available_models()`, which already returns the correct list for the active backend. No changes needed to `/model` — it works correctly once the backend is set by the atomic flow.

### Phase 2.5: Future-Proof Backend Registry
To ensure adding new APIs or CLIs requires minimal code changes:
1.  **Backend Registry Pattern**: Introduce a lightweight `BACKEND_REGISTRY` dict that maps backend names to their metadata: adapter class, credential key pattern, available models list, and default model. New backends are added by registering one entry — no scattered if/elif branches.
2.  **Adapter Interface**: Formalize the implicit adapter contract (currently `get_backend_class()` returns different classes). Each adapter must implement `init()`, `send()`, `shutdown()` — this is already the case in practice, just make it explicit with a base class or protocol.
3.  **Model list as data, not code**: Move `AVAILABLE_OPENROUTER_MODELS` and equivalent CLI model lists into the registry or a `models.json` config file. Adding a model = editing data, not code.

### Phase 3: Dynamic Model Discovery (Optional/Deferred)
Add a background task to `FlexibleAgentRuntime` startup:
1.  **Probe**: Fetch `https://openrouter.ai/api/v1/models` on startup.
2.  **Cache**: Store result in memory; fallback to hardcoded `AVAILABLE_OPENROUTER_MODELS` if network fails.

> **Recommendation**: Defer indefinitely. OpenRouter returns hundreds of models — the UX of scrolling through them on a Telegram keyboard is poor. The curated hardcoded list is better UX. Update it manually when needed.

---

## 4. Risks & Mitigations

| Risk | Mitigation |
| :--- | :--- |
| **Network Latency** | Use async non-blocking probes for model discovery. |
| **Format Mismatch** | `DEFAULT_MODEL_PER_BACKEND` dict auto-selects valid model during backend switch (Phase 2). |
| **API Rate Limits** | Per-agent key support (Phase 1) isolates usage. |
| **Handoff context lost during two-stage switch** | `/backend +` flag is preserved through the chained keyboard and applied after model confirmation (Phase 2). |
| **New backend requires scattered code changes** | Backend Registry pattern (Phase 2.5) centralizes all backend metadata — new backends = one registry entry + one adapter class. |

---

## 5. Validation Matrix

All switch paths must be tested before merge:

| From → To | Stateless | With `+` handoff |
| :--- | :--- | :--- |
| CLI → CLI (e.g. gemini → claude) | ✅ | ✅ |
| CLI → API (e.g. gemini → openrouter) | ✅ | ✅ |
| API → CLI (e.g. openrouter → gemini) | ✅ | ✅ |
| API → API (if applicable) | ✅ | ✅ |

Each test must verify: backend initializes, model is valid for new backend, rollback works if init fails, `/model` shows correct list after switch.

---

## 6. Implementation Outcome

Implemented behavior:

1. Tiered OpenRouter credential loading now checks:
   - `{agent_name}_openrouter_key`
   - `openrouter-api_key`
   - `openrouter_key`
2. `/backend` now opens a model picker and commits only after model confirmation.
3. `/backend +` preserves the handoff flag through the picker flow and applies handoff after the switch succeeds.
4. Flex workspace `state.json` now persists:
   - `active_backend`
   - per-backend `model`
   - per-backend `effort`
5. Backend metadata is centralized in `orchestrator/flexible_backend_registry.py`.
6. OpenRouter default model is standardized on `anthropic/claude-sonnet-4.6`.

User-facing usage:

```text
/backend
```

- choose target backend
- choose model
- bridge commits switch only after model selection

```text
/backend openrouter-api +
```

- opens the OpenRouter model picker
- after confirmation, switch occurs and handoff context is prepared

```text
/model
/effort
```

- `/model` switches models within the current active backend
- `/effort` changes reasoning effort on supported backends
- both persist into flex `state.json`

## 7. Next Steps

1.  **User Review**: 爸爸 (Owner) approves the architectural approach.
2.  **Build Phase 1**: Tiered credential loading.
3.  **Build Phase 2 + 2.5**: Atomic UI switching with model mapper, `/backend +` preservation, and backend registry.
4.  **Validation**: Execute the full test matrix (Section 5) across all switch paths.

---
*Drafted by 小夏 (Sunny) for 爸爸's review.* 🌸✨
