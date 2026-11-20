"""Request-local HERV3 tool presentation; never a permission/authority layer.

Small models see a compact default set. Calling tool_find changes the *next*
model payload to show relevant existing tool schemas. The PAO registry still
owns every allowlist, availability check and execution admission.
"""

from __future__ import annotations

import json
from typing import Any

from tools.developer_toolkit import _tool_find_selections

_STARTER_TOOLS = (
    "tool_find", "project_locate", "code_search", "git_inspect",
    "file_manage", "text_replace", "data_inspect", "file_read", "shell",
    "ask_user", "get_user_answer",
)

# Bounded request-local presentation: later tool_find discoveries accumulate
# onto earlier ones (never silently drop an already-revealed, still-authorised
# schema), capped so the model payload stays compact for small models.
_MAX_DISCOVERED = 48


class OnDemandToolView:
    """Only schemas change. Every other operation delegates to the same registry."""

    def __init__(self, base: Any, *, goal: str = "") -> None:
        self._base = base
        self.max_loops = None
        self._discovered: set[str] = set()
        # Relevance hints are advisory, not new immutable capability state.
        available = tuple(
            str((schema.get("function") or {}).get("name") or "")
            for schema in base.get_tool_definitions(tiers=None)
        )
        self._starter = set(_STARTER_TOOLS).intersection(available)
        if goal:
            self._starter.update(
                _tool_find_selections({"query": goal, "limit": 4}, available)
            )

    @property
    def base(self) -> Any:
        return getattr(self._base, "base", self._base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def get_tool_definitions(self, tiers=None) -> list[dict]:
        # Ignore Ollama's historical ["core"] presentation tier *only* in
        # this opt-in view; the original registry still validates permission.
        all_definitions = self._base.get_tool_definitions(tiers=None)
        visible = self._starter | self._discovered
        return [
            item for item in all_definitions
            if str((item.get("function") or {}).get("name") or "") in visible
        ]

    def _activate_after_success(self, tool_name: str, arguments: dict, result: Any) -> None:
        if tool_name != "tool_find" or getattr(result, "is_error", False):
            return
        output = str(getattr(result, "output", "") or "")
        try:
            payload = json.loads(output)
        except (ValueError, TypeError):
            # Smart Registry may wrap the output; compute only from the
            # already-admitted, registry-visible schema names below.
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            return
        available = tuple(
            str((schema.get("function") or {}).get("name") or "")
            for schema in self._base.get_tool_definitions(tiers=None)
        )
        revealed = set(_tool_find_selections(arguments, available))
        # Accumulate within this request and keep only still-authorised names.
        self._discovered = (self._discovered | revealed) & set(available)
        if len(self._discovered) > _MAX_DISCOVERED:
            self._discovered = set(sorted(self._discovered)[:_MAX_DISCOVERED])

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = ""):
        result = await self._base.execute(tool_name, arguments, tool_call_id)
        self._activate_after_success(tool_name, arguments, result)
        return result

    async def execute_with_audit_context(
        self, tool_name: str, arguments: dict, tool_call_id: str = "",
        *, audit_context: dict | None = None,
    ):
        execute = getattr(self._base, "execute_with_audit_context", None)
        if callable(execute):
            result = await execute(
                tool_name, arguments, tool_call_id, audit_context=audit_context
            )
        else:
            result = await self._base.execute(tool_name, arguments, tool_call_id)
        self._activate_after_success(tool_name, arguments, result)
        return result


def on_demand_requested(
    tool_options: dict | None, *, model: str, is_herv3: bool,
) -> bool:
    """Explicit non-production opt-in. No model changes without configuration."""
    if not is_herv3 or not isinstance(tool_options, dict):
        return False
    options = tool_options.get("herv3_tool_presentation")
    return isinstance(options, dict) and options.get("mode") == "on_demand"


def developer_toolkit_guidance_enabled(
    tool_options: dict | None,
    tool_names: set[str] | frozenset[str],
    *,
    model: str,
    is_herv3: bool,
) -> bool:
    """Enable optional HERV3 Developer Toolkit guidance only on explicit opt-in."""
    if not is_herv3:
        return False
    options = (
        tool_options.get("herv3_tool_presentation")
        if isinstance(tool_options, dict)
        else None
    )
    if isinstance(options, dict):
        mode = str(options.get("mode") or "").strip().casefold()
        if mode in {"off", "disabled", "none", "false"}:
            return False
    if on_demand_requested(tool_options, model=model, is_herv3=is_herv3):
        return True
    from tools.developer_toolkit_schemas import TOOLKIT_TOOL_NAMES

    return bool(set(tool_names) & set(TOOLKIT_TOOL_NAMES))
