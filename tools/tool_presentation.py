"""Request-local HERV3 tool presentation; never a permission/authority layer.

Small models see a compact default set. Calling tool_find changes the *next*
model payload to show relevant existing tool schemas. The PAO registry still
owns every allowlist, availability check and execution admission.
"""

from __future__ import annotations

from collections import OrderedDict
import json
from typing import Any

from tools.developer_toolkit import _tool_find_selections

_STARTER_TOOLS = (
    "tool_find", "project_locate", "code_search", "git_inspect",
    "file_manage", "text_replace", "data_inspect", "file_read", "file_write",
    "shell", "ask_user", "get_user_answer",
)

# Bounded request-local presentation: later tool_find discoveries accumulate
# onto earlier ones (never silently drop an already-revealed, still-authorised
# schema), capped so the model payload stays compact for small models.
_MAX_DISCOVERED = 48
# In-use tools are also bounded with the same LRU strategy so a very long
# tool-enabled turn cannot grow the exposed schemas without limit.
_MAX_IN_USE = 48


class OnDemandToolView:
    """Only schemas change. Every other operation delegates to the same registry."""

    def __init__(self, base: Any, *, goal: str = "") -> None:
        self._base = base
        self.max_loops = None
        # Ordered LRU map so tools are retained by recency/usage rather than
        # alphabetically evicting names like 'windows_*' / 'z*' above 48.
        self._discovered: OrderedDict[str, bool] = OrderedDict()
        self._in_use: OrderedDict[str, bool] = OrderedDict()
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
        visible = self._starter | set(self._discovered.keys()) | set(self._in_use.keys())
        return [
            item for item in all_definitions
            if str((item.get("function") or {}).get("name") or "") in visible
        ]

    def _note_used(self, tool_name: str) -> None:
        if tool_name and tool_name not in self._starter:
            self._in_use[tool_name] = True
            self._in_use.move_to_end(tool_name)
            if tool_name in self._discovered:
                self._discovered.move_to_end(tool_name)
            # Bounded LRU: evict the least-recently-used in-use tool so a long
            # turn never expands the exposed schemas indefinitely. The just-used
            # tool sits at the MRU end and is never the eviction victim.
            while len(self._in_use) > _MAX_IN_USE:
                evict = next(iter(self._in_use))
                del self._in_use[evict]

    def _activate_after_success(self, tool_name: str, arguments: dict, result: Any) -> None:
        self._note_used(tool_name)
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
        available_set = set(available)
        for name in list(self._discovered):
            if name not in available_set:
                del self._discovered[name]
        for name in _tool_find_selections(arguments, available):
            if name in available_set:
                self._discovered[name] = True
                self._discovered.move_to_end(name)
        while len(self._discovered) > _MAX_DISCOVERED:
            evict = next((k for k in self._discovered if k not in self._in_use), None)
            if evict is None:
                break
            del self._discovered[evict]

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


_ON_DEMAND_DISABLED_MODES = frozenset({"full", "off", "disabled", "none", "false"})
_GUIDANCE_DISABLED_MODES = frozenset({"off", "disabled", "none", "false"})


def on_demand_requested(
    tool_options: dict | None, *, model: str, is_herv3: bool,
) -> bool:
    """Default ON for HERV3; opt out with herv3_tool_presentation.mode='full'|'off'."""
    if not is_herv3:
        return False
    if isinstance(tool_options, dict):
        options = tool_options.get("herv3_tool_presentation")
        if isinstance(options, dict):
            mode = str(options.get("mode") or "").strip().casefold()
            if mode in _ON_DEMAND_DISABLED_MODES:
                return False
            if mode == "on_demand":
                return True
    return True


def developer_toolkit_guidance_enabled(
    tool_options: dict | None,
    tool_names: set[str] | frozenset[str],
    *,
    model: str,
    is_herv3: bool,
) -> bool:
    """Enable HERV3 Developer Toolkit guidance whenever toolkit tools are present."""
    if not is_herv3:
        return False
    options = (
        tool_options.get("herv3_tool_presentation")
        if isinstance(tool_options, dict)
        else None
    )
    if isinstance(options, dict):
        mode = str(options.get("mode") or "").strip().casefold()
        if mode in _GUIDANCE_DISABLED_MODES:
            return False
    from tools.developer_toolkit_schemas import TOOLKIT_TOOL_NAMES

    return bool(set(tool_names) & set(TOOLKIT_TOOL_NAMES))
