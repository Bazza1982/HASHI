"""meter_cost.py — Per-call cost line items and deterministic /meter rendering.

Implements the data contract approved by Zelda for the ``/meter`` (``/metre``)
turn-cost display feature.  This module is intentionally model-free and
provider-free: it never calls an LLM and never performs I/O, so rendering a
cost tail can never itself accrue cost.

Data contract (one line item per provider/stage invocation):
  request_id, parent_request_id, phase, engine, model, input/output/thinking
  tokens, token_source, cost_usd, cost_source, optional prompt-cache hit/miss
  tokens, and optional provider-call latency.

``cost_usd`` uses ``None`` for "unknown" and is strictly distinguished from a
genuine ``0.0`` (local / free model).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PerCallUsageLineItem",
    "UsageReceipt",
    "format_cost_tail",
    "format_meditation_cost_tail",
    "receipt_from_line_items",
    "line_item_from_dict",
]


@dataclass
class PerCallUsageLineItem:
    """One provider/stage invocation with phase, engine, model and provenance."""

    request_id: str = ""
    parent_request_id: str = ""
    phase: str = ""          # execution / review / persona / wrapper / meditation / dream / …
    engine: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    token_source: str = "estimated"   # provider / estimated
    # Provider APIs report reasoning/thinking as a detail of output tokens.
    # Locally estimated thinking is separate from estimated visible output.
    thinking_in_output: bool = False
    cost_usd: float | None = None
    cost_source: str = "unknown"      # provider / pricing_table / local_zero / unknown
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    provider_call_latency_ms: float | None = None

    @property
    def total_tokens(self) -> int:
        extra_thinking = 0 if self.thinking_in_output else self.thinking_tokens
        return self.input_tokens + self.output_tokens + extra_thinking

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "parent_request_id": self.parent_request_id,
            "phase": self.phase,
            "engine": self.engine,
            "model": self.model,
            "input": self.input_tokens,
            "output": self.output_tokens,
            "thinking": self.thinking_tokens,
            "token_source": self.token_source,
            "thinking_in_output": self.thinking_in_output,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            "provider_call_latency_ms": self.provider_call_latency_ms,
        }


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _optional_nonnegative_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(max(0.0, float(value)), 3)
    except (TypeError, ValueError):
        return None


def line_item_from_dict(data: dict[str, Any]) -> PerCallUsageLineItem:
    token_source = str(data.get("token_source") or "estimated")
    raw_thinking_in_output = data.get("thinking_in_output")
    thinking_in_output = (
        bool(raw_thinking_in_output)
        if raw_thinking_in_output is not None
        else token_source == "provider"
    )
    return PerCallUsageLineItem(
        request_id=str(data.get("request_id") or ""),
        parent_request_id=str(data.get("parent_request_id") or ""),
        phase=str(data.get("phase") or ""),
        engine=str(data.get("engine") or ""),
        model=str(data.get("model") or ""),
        input_tokens=int(data.get("input") or 0),
        output_tokens=int(data.get("output") or 0),
        thinking_tokens=int(data.get("thinking") or 0),
        token_source=token_source,
        thinking_in_output=thinking_in_output,
        cost_usd=data.get("cost_usd"),
        cost_source=str(data.get("cost_source") or "unknown"),
        prompt_cache_hit_tokens=_optional_nonnegative_int(
            data.get("prompt_cache_hit_tokens")
        ),
        prompt_cache_miss_tokens=_optional_nonnegative_int(
            data.get("prompt_cache_miss_tokens")
        ),
        provider_call_latency_ms=_optional_nonnegative_float(
            data.get("provider_call_latency_ms")
        ),
    )


@dataclass
class UsageReceipt:
    """Structured, per-call usage receipt returned by :func:`record_usage`."""

    request_id: str = ""
    parent_request_id: str = ""
    line_items: list[PerCallUsageLineItem] = field(default_factory=list)

    @property
    def total_input(self) -> int:
        return sum(li.input_tokens for li in self.line_items)

    @property
    def total_output(self) -> int:
        return sum(li.output_tokens for li in self.line_items)

    @property
    def total_thinking(self) -> int:
        return sum(li.thinking_tokens for li in self.line_items)

    @property
    def total_tokens(self) -> int:
        return sum(li.total_tokens for li in self.line_items)

    @property
    def cost_usd(self) -> float | None:
        """Aggregate cost, or ``None`` when any component cost is unknown."""
        if not self.line_items:
            return None
        known: list[float] = []
        for li in self.line_items:
            if li.cost_usd is None:
                return None
            known.append(float(li.cost_usd))
        return round(sum(known), 6)

    @property
    def has_local_only(self) -> bool:
        return bool(self.line_items) and all(
            li.cost_source == "local_zero" for li in self.line_items
        )

    def dominant_cost_source(self) -> str:
        if not self.line_items:
            return "unknown"
        sources = {li.cost_source for li in self.line_items}
        if sources <= {"local_zero"}:
            return "local_zero"
        if "unknown" in sources:
            return "unknown"
        if "provider" in sources and "pricing_table" not in sources:
            return "provider"
        return "pricing_table"


def receipt_from_line_items(
    line_items: list[PerCallUsageLineItem],
    *,
    request_id: str = "",
    parent_request_id: str = "",
) -> UsageReceipt:
    return UsageReceipt(
        request_id=request_id,
        parent_request_id=parent_request_id,
        line_items=list(line_items),
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_cost(cost_usd: float) -> str:
    if cost_usd < 0.0001:
        return "< US$0.0001"
    if cost_usd < 0.01:
        return f"US${cost_usd:.6f}"
    return f"US${cost_usd:.4f}"


def format_cost_tail(
    receipt: UsageReceipt,
    *,
    label: str = "前台回合",
    task_total_usd: float | None = None,
) -> str:
    """Deterministically render a cost tail from a receipt (no model call)."""
    cost = receipt.cost_usd
    tokens = receipt.total_tokens
    tokens_text = _fmt_tokens(tokens)
    if cost is None:
        return f"💰 {label}：成本未知 · {tokens_text} tokens"
    if receipt.has_local_only:
        cost_text = "US$0 · 本地模型"
        return f"💰 {label}：{cost_text} · {tokens_text} tokens"
    source = receipt.dominant_cost_source()
    if source == "provider":
        body = f"{_fmt_cost(cost)} · {tokens_text} tokens · Provider 实报"
    else:
        body = f"≈ {_fmt_cost(cost)} · {tokens_text} tokens · 价目表估算"
    line = f"💰 {label}：{body}"
    if task_total_usd is not None:
        line += f" · 任务累计 ≈ {_fmt_cost(task_total_usd)}"
    return line


def format_meditation_cost_tail(
    receipt: UsageReceipt,
    *,
    task_total_usd: float | None = None,
) -> str:
    """Deterministically render the asynchronous Meditation cost tail.

    Distinct from :func:`format_cost_tail` only in its fixed 🧘 label; the
    Meditation stage finishes after the foreground answer and is never folded
    into the foreground tail, so it gets its own short, model-free message.
    """
    cost = receipt.cost_usd
    tokens = receipt.total_tokens
    tokens_text = _fmt_tokens(tokens)
    if cost is None:
        line = f"🧘 冥想：成本未知 · {tokens_text} tokens"
    elif receipt.has_local_only:
        line = f"🧘 冥想：US$0 · 本地模型 · {tokens_text} tokens"
    else:
        source = receipt.dominant_cost_source()
        if source == "provider":
            line = f"🧘 冥想：{_fmt_cost(cost)} · {tokens_text} tokens · Provider 实报"
        else:
            line = f"🧘 冥想：≈ {_fmt_cost(cost)} · {tokens_text} tokens · 价目表估算"
    if task_total_usd is not None:
        line += f" · 任务累计 ≈ {_fmt_cost(task_total_usd)}"
    return line
