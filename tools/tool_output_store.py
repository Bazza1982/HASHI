"""Run-bound in-process retention for oversized tool results.

These references are derived from already-authorized tool results. They do not
grant access to any other Run or authorize a new filesystem/network operation.
Retention lasts for this Registry instance; worker replacement clears it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping


def run_binding(context: Mapping[str, Any]) -> tuple[str, ...] | None:
    run_id = str(context.get("hashi_run_id") or context.get("run_id") or "")
    request_id = str(context.get("request_id") or "")
    if not run_id and not request_id:
        return None
    return tuple(
        str(context.get(key) or "")
        for key in ("instance_id", "agent_name", "owner_id", "hashi_session_id")
    ) + (run_id, request_id)


@dataclass(frozen=True)
class RetainedOutput:
    binding: tuple[str, ...]
    source_tool: str
    text: str
    digest: str
    kind: str = "text"
    image_outcomes: dict[int, bool] = field(default_factory=dict, compare=False)


class ToolOutputStore:
    def __init__(self) -> None:
        self._items: dict[str, RetainedOutput] = {}
        self._lock = threading.Lock()

    def retain(
        self, binding: tuple[str, ...] | None, source_tool: str, text: str, *,
        kind: str = "text", image_outcomes: Mapping[int, bool] | None = None,
    ) -> str | None:
        if binding is None:
            return None
        digest = hashlib.sha256()
        for start in range(0, len(text), 64 * 1024):
            digest.update(text[start:start + 64 * 1024].encode("utf-8", errors="surrogatepass"))
        reference = secrets.token_urlsafe(32)
        item = RetainedOutput(binding, source_tool, text, digest.hexdigest(), kind, dict(image_outcomes or {}))
        with self._lock:
            self._items[reference] = item
        return reference

    def lookup(self, binding: tuple[str, ...] | None, reference: Any) -> RetainedOutput:
        if binding is None or not isinstance(reference, str) or not reference or len(reference) > 128:
            raise ValueError("result reference is invalid or belongs to another Run")
        with self._lock:
            item = self._items.get(reference)
        if item is None or item.binding != binding:
            raise ValueError("result reference is invalid or belongs to another Run")
        return item

    def record_image_outcomes(
        self, binding: tuple[str, ...] | None, reference: Any,
        outcomes: Mapping[int, bool], total: int,
    ) -> bool:
        item = self.lookup(binding, reference)
        if item.kind != "legacy_screenshot" or total < 0:
            raise ValueError("result reference is not a screenshot page")
        with self._lock:
            for index, valid in outcomes.items():
                if type(index) is not int or index < 0 or index >= total or type(valid) is not bool:
                    raise ValueError("invalid screenshot page outcome")
                item.image_outcomes[index] = valid
            return len(item.image_outcomes) == total and all(item.image_outcomes.values())

    def has_bound(self, binding: tuple[str, ...] | None) -> bool:
        if binding is None:
            return False
        with self._lock:
            return any(item.binding == binding for item in self._items.values())

    def source_allowed(self, binding: tuple[str, ...] | None, reference: Any, allowed: set[str]) -> bool:
        try:
            return self.lookup(binding, reference).source_tool in allowed
        except ValueError:
            return False

    def page(self, binding: tuple[str, ...] | None, reference: Any, offset: Any, limit: Any = None) -> str:
        item = self.lookup(binding, reference)
        if type(offset) is not int or offset < 0 or offset > len(item.text):
            raise ValueError("invalid result offset")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("invalid result page limit")
        # JSON control-character escapes may be six characters long. Choose a
        # page that stays below the shared model-visible output budget.
        low = offset
        high = min(len(item.text), offset + min(limit if limit is not None else 10_000, 10_000))

        def render(end: int) -> str:
            complete = end == len(item.text)
            return json.dumps({
                "reference": reference,
                "offset": offset,
                "text": item.text[offset:end],
                "next_offset": None if complete else end,
                "complete": complete,
                "sha256": item.digest,
                "retention": "registry_lifetime",
            }, ensure_ascii=False, separators=(",", ":"))

        while low < high:
            middle = (low + high + 1) // 2
            if len(render(middle)) <= 15_000:
                low = middle
            else:
                high = middle - 1
        return render(low)
# HASHI-Fingerprint: hfp_921ad73b3593472ab571155fe338d46b
