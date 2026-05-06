from __future__ import annotations

"""Compatibility exports for the legacy fixed runtime.

New code should import shared primitives from the focused modules:

- `orchestrator.runtime_common`
- `orchestrator.runtime_jobs`
- `orchestrator.runtime_display`
- `orchestrator.model_catalog`

`BridgeAgentRuntime` remains available here while fixed-runtime callers are
audited and migrated.
"""

from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime
from orchestrator.model_catalog import (
    AVAILABLE_CLAUDE_EFFORTS,
    AVAILABLE_CLAUDE_MODELS,
    AVAILABLE_CODEX_EFFORTS,
    AVAILABLE_CODEX_MODELS,
    AVAILABLE_GEMINI_MODELS,
    AVAILABLE_OPENROUTER_MODELS,
    CLAUDE_MODEL_ALIASES,
)
from orchestrator.runtime_common import (
    QueuedRequest,
    _md_to_html,
    _print_final_response,
    _print_thinking,
    _print_user_message,
    _safe_excerpt,
    resolve_authorized_telegram_ids,
)
from orchestrator.runtime_display import _show_logo_animation
from orchestrator.runtime_jobs import _build_jobs_text, _build_jobs_with_buttons

__all__ = [
    "AVAILABLE_CLAUDE_EFFORTS",
    "AVAILABLE_CLAUDE_MODELS",
    "AVAILABLE_CODEX_EFFORTS",
    "AVAILABLE_CODEX_MODELS",
    "AVAILABLE_GEMINI_MODELS",
    "AVAILABLE_OPENROUTER_MODELS",
    "BridgeAgentRuntime",
    "CLAUDE_MODEL_ALIASES",
    "QueuedRequest",
    "_build_jobs_text",
    "_build_jobs_with_buttons",
    "_md_to_html",
    "_print_final_response",
    "_print_thinking",
    "_print_user_message",
    "_safe_excerpt",
    "_show_logo_animation",
    "resolve_authorized_telegram_ids",
]
