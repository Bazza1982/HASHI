"""meter_cost.py — Per-call cost line items and deterministic /meter rendering.

Implements the data contract approved by Zelda for the ``/meter`` (``/metre``)
turn-cost display feature.  This module is intentionally model-free and
provider-free: it never calls an LLM or a Provider. Rendering may load the
static UI language catalogue and price table, but can never itself accrue
model cost.

Data contract (one line item per provider/stage invocation):
  request_id, parent_request_id, phase, engine, model, input/output/thinking
  tokens, token_source, cost_usd, cost_source, optional prompt-cache hit/miss
  tokens, and optional provider-call latency.

``cost_usd`` uses ``None`` for "unknown" and is strictly distinguished from a
genuine ``0.0`` (local / free model).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
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
    provider_request_id: str = ""
    attempt: int = 1
    retry_count: int = 0
    recovery_kind: str = "none"
    compact: bool = False
    routing_revision: int = 1
    capability_revision: int = 1
    pricing_revision: str = "unknown"
    status: str = "completed"

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
            "provider_request_id": self.provider_request_id,
            "attempt": self.attempt,
            "retry_count": self.retry_count,
            "recovery_kind": self.recovery_kind,
            "compact": self.compact,
            "routing_revision": self.routing_revision,
            "capability_revision": self.capability_revision,
            "pricing_revision": self.pricing_revision,
            "status": self.status,
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
        provider_request_id=str(data.get("provider_request_id") or ""),
        attempt=max(1, int(data.get("attempt") or 1)),
        retry_count=max(0, int(data.get("retry_count") or 0)),
        recovery_kind=str(data.get("recovery_kind") or "none"),
        compact=bool(data.get("compact", False)),
        routing_revision=max(1, int(data.get("routing_revision") or 1)),
        capability_revision=max(1, int(data.get("capability_revision") or 1)),
        pricing_revision=str(data.get("pricing_revision") or "unknown"),
        status=str(data.get("status") or "completed"),
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
    def provider_request_count(self) -> int:
        """Number of physical Provider/stage calls represented by the receipt."""

        return len(self.line_items)

    @property
    def thinking_in_output_tokens(self) -> int:
        """Reasoning tokens already included in Provider output token counts."""

        return sum(
            int(getattr(li, "thinking_tokens", 0) or 0)
            for li in self.line_items
            if bool(getattr(li, "thinking_in_output", False))
        )

    @property
    def separate_thinking_tokens(self) -> int:
        """Locally estimated reasoning tokens not included in output tokens."""

        return sum(
            int(getattr(li, "thinking_tokens", 0) or 0)
            for li in self.line_items
            if not bool(getattr(li, "thinking_in_output", False))
        )

    @property
    def cache_metrics_complete(self) -> bool:
        """Whether every input-bearing call reported cache hit and miss counts."""

        return bool(self.line_items) and all(
            int(getattr(li, "input_tokens", 0) or 0) <= 0
            or (
                getattr(li, "prompt_cache_hit_tokens", None) is not None
                and getattr(li, "prompt_cache_miss_tokens", None) is not None
            )
            for li in self.line_items
        )

    @property
    def prompt_cache_hit_tokens(self) -> int | None:
        if not self.cache_metrics_complete:
            return None
        return sum(
            int(getattr(li, "prompt_cache_hit_tokens", 0) or 0)
            for li in self.line_items
        )

    @property
    def prompt_cache_miss_tokens(self) -> int | None:
        if not self.cache_metrics_complete:
            return None
        return sum(
            int(getattr(li, "prompt_cache_miss_tokens", 0) or 0)
            for li in self.line_items
        )

    @property
    def cache_hit_percent(self) -> float | None:
        hits = self.prompt_cache_hit_tokens
        if hits is None or self.total_input <= 0:
            return None
        return max(0.0, min(100.0, hits * 100.0 / self.total_input))

    def _pricebook_cost(self, *, use_observed_cache: bool) -> float | None:
        """Reprice every line item with the active table, preserving call tiers."""

        if not self.line_items:
            return None
        if use_observed_cache and not self.cache_metrics_complete:
            return None
        # Lazy import avoids a module cycle: token_tracker imports this module
        # only while constructing a structured receipt.
        from tools.token_tracker import resolve_usage_cost

        total = 0.0
        for item in self.line_items:
            if getattr(item, "cost_source", "unknown") == "local_zero":
                continue
            model = str(getattr(item, "model", "") or "")
            engine = str(getattr(item, "engine", "") or "")
            cached_tokens = (
                int(getattr(item, "prompt_cache_hit_tokens", 0) or 0)
                if use_observed_cache
                else 0
            )
            cost, source, _revision = resolve_usage_cost(
                cost_usd=None,
                model=model,
                engine=engine,
                input_tokens=int(getattr(item, "input_tokens", 0) or 0),
                output_tokens=int(getattr(item, "output_tokens", 0) or 0),
                thinking_tokens=int(getattr(item, "thinking_tokens", 0) or 0),
                cached_tokens=cached_tokens,
                thinking_in_output=bool(
                    getattr(item, "thinking_in_output", False)
                ),
            )
            if cost is None or source != "pricing_table":
                return None
            total += cost
        return round(total, 6)

    @property
    def no_cache_cost_usd(self) -> float | None:
        """Pricebook estimate if none of the observed input had hit cache."""

        return self._pricebook_cost(use_observed_cache=False)

    @property
    def cache_savings_usd(self) -> float | None:
        """Pricebook-only cache savings; never mixes Provider actuals and estimates."""

        with_cache = self._pricebook_cost(use_observed_cache=True)
        without_cache = self.no_cache_cost_usd
        if with_cache is None or without_cache is None:
            return None
        return round(max(0.0, without_cache - with_cache), 6)

    @property
    def cache_savings_percent(self) -> float | None:
        savings = self.cache_savings_usd
        without_cache = self.no_cache_cost_usd
        if savings is None or without_cache is None or without_cache <= 0:
            return None
        return max(0.0, min(100.0, savings * 100.0 / without_cache))

    @property
    def pricing_revisions(self) -> tuple[str, ...]:
        revisions = sorted(
            {
                str(getattr(item, "pricing_revision", "unknown") or "").strip()
                for item in self.line_items
                if str(
                    getattr(item, "pricing_revision", "unknown") or ""
                ).strip().casefold()
                not in {"", "unknown"}
            }
        )
        if revisions:
            return tuple(revisions)
        if self.dominant_cost_source() == "pricing_table":
            from tools.token_tracker import PRICING_REVISION

            return (PRICING_REVISION,)
        return ()

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
        return f"{n/1_000_000:.3f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_cost(cost_usd: float, *, locale: str | None) -> str:
    """Render compact cents below US$1 and USD at or above that threshold."""

    if cost_usd < 1.0:
        amount = "0" if cost_usd <= 0 else f"{cost_usd * 100:.2f}"
        return _translate(
            "meter.tail.cost.cents",
            locale=locale,
            amount=amount,
        )
    amount = f"{cost_usd:.4f}".rstrip("0").rstrip(".")
    if "." not in amount:
        amount += ".00"
    elif len(amount.rsplit(".", 1)[1]) == 1:
        amount += "0"
    return _translate(
        "meter.tail.cost.usd",
        locale=locale,
        amount=amount,
    )


def _translate(key: str, *, locale: str | None = None, **values: Any) -> str:
    """Resolve all user-visible meter prose through the runtime language pack."""

    from orchestrator import ui_language

    return ui_language.tr(key, locale=locale, **values)


def _fmt_duration(seconds: float, *, locale: str | None) -> str:
    """Render a compact human duration without exposing stopwatch precision."""

    elapsed_s = max(0.0, float(seconds))
    if elapsed_s == 0:
        return _translate("meter.tail.duration.seconds", locale=locale, amount="0")
    if elapsed_s < 0.05:
        return _translate("meter.tail.duration.lt_tenth", locale=locale)
    if elapsed_s < 60:
        amount = f"{elapsed_s:.1f}".rstrip("0").rstrip(".")
        return _translate(
            "meter.tail.duration.seconds", locale=locale, amount=amount
        )
    whole_seconds = round(elapsed_s)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return _translate(
            "meter.tail.duration.hours_minutes",
            locale=locale,
            hours=hours,
            minutes=minutes,
        )
    return _translate(
        "meter.tail.duration.minutes_seconds",
        locale=locale,
        minutes=minutes,
        seconds=remaining_seconds,
    )


_USER_STAGE_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("strategy", ("triage",)),
    ("planning", ("planning",)),
    ("execution", ("direct", "execution")),
)


def _format_timing_lines(
    *,
    total_elapsed_s: float | None,
    stage_timings_s: Mapping[str, float] | None,
    locale: str | None,
) -> tuple[str, ...]:
    lines: list[str] = []
    if total_elapsed_s is not None:
        try:
            elapsed_s = float(total_elapsed_s)
        except (TypeError, ValueError):
            elapsed_s = -1.0
        if math.isfinite(elapsed_s) and elapsed_s >= 0:
            lines.append(
                _translate(
                    "meter.tail.elapsed",
                    locale=locale,
                    duration=_fmt_duration(elapsed_s, locale=locale),
                )
            )

    if not isinstance(stage_timings_s, Mapping):
        return tuple(lines)
    stage_items: list[str] = []
    for label_key, raw_stages in _USER_STAGE_BUCKETS:
        elapsed_s = 0.0
        observed = False
        for raw_stage in raw_stages:
            raw_value = stage_timings_s.get(raw_stage)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or value < 0:
                continue
            observed = True
            elapsed_s += value
        if not observed:
            continue
        stage_items.append(
            _translate(
                "meter.tail.stage.item",
                locale=locale,
                label=_translate(
                    f"meter.tail.stage.{label_key}", locale=locale
                ),
                duration=_fmt_duration(elapsed_s, locale=locale),
            )
        )
    if stage_items:
        lines.append(
            _translate(
                "meter.tail.stages",
                locale=locale,
                stages=" · ".join(stage_items),
            )
        )
    return tuple(lines)


_PROVIDER_DISPLAY_NAMES = {
    "hashi-api": "HASHI API",
    "hashi": "HASHI API",
    "deepseek-api": "DeepSeek",
    "deepseek": "DeepSeek",
    "openrouter-api": "OpenRouter",
    "openrouter": "OpenRouter",
    "codex-cli": "Codex CLI",
    "claude-cli": "Claude CLI",
    "gemini-cli": "Gemini CLI",
    "grok-cli": "Grok CLI",
    "ollama-api": "Ollama",
    "xai-api": "xAI API",
    "xai": "xAI",
    "hashi-runtime": "HASHI Runtime",
}


def _format_provider_names(
    receipt: UsageReceipt,
    *,
    locale: str | None,
) -> str:
    """Render the physical provider engines represented by a receipt.

    HER v2 may route different stages through different providers, so this is
    deliberately derived from every line item instead of the top-level
    backend.  Preserve first-use order while folding aliases that share the
    same user-facing brand name.
    """

    names: list[str] = []
    seen: set[str] = set()
    for item in receipt.line_items:
        engine = str(getattr(item, "engine", "") or "").strip()
        if not engine:
            continue
        display_name = _PROVIDER_DISPLAY_NAMES.get(engine.casefold(), engine)
        dedupe_key = display_name.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        names.append(display_name)
    if not names:
        return _translate("meter.tail.provider.unknown", locale=locale)
    return " + ".join(names)


def _format_receipt_lines(
    receipt: UsageReceipt,
    *,
    icon: str,
    label_key: str,
    label: str | None,
    locale: str | None,
    task_total_usd: float | None,
    total_elapsed_s: float | None,
    stage_timings_s: Mapping[str, float] | None,
) -> str:
    cost = receipt.cost_usd
    resolved_label = label or _translate(label_key, locale=locale)
    if cost is None:
        cost_line = _translate(
            "meter.tail.cost.unknown",
            locale=locale,
            icon=icon,
            label=resolved_label,
        )
    elif receipt.has_local_only:
        cost_line = _translate(
            "meter.tail.cost.local",
            locale=locale,
            icon=icon,
            label=resolved_label,
            cost=_fmt_cost(0.0, locale=locale),
        )
    elif receipt.dominant_cost_source() == "provider":
        cost_line = _translate(
            (
                "meter.tail.cost.provider_priced"
                if receipt.pricing_revisions
                else "meter.tail.cost.provider"
            ),
            locale=locale,
            icon=icon,
            label=resolved_label,
            cost=_fmt_cost(cost, locale=locale),
        )
    else:
        cost_line = _translate(
            "meter.tail.cost.pricing",
            locale=locale,
            icon=icon,
            label=resolved_label,
            cost=_fmt_cost(cost, locale=locale),
        )
    if task_total_usd is not None:
        cost_line += _translate(
            "meter.tail.task_total",
            locale=locale,
            cost=_fmt_cost(task_total_usd, locale=locale),
        )
    cost_line += _translate(
        "meter.tail.provider",
        locale=locale,
        providers=_format_provider_names(receipt, locale=locale),
    )

    cache_hit = receipt.prompt_cache_hit_tokens
    cache_rate = receipt.cache_hit_percent
    if cache_hit is not None and cache_rate is not None:
        input_line = _translate(
            "meter.tail.input.cache",
            locale=locale,
            input_tokens=_fmt_tokens(receipt.total_input),
            cache_hit_tokens=_fmt_tokens(cache_hit),
            cache_hit_percent=f"{cache_rate:.1f}",
        )
    else:
        input_line = _translate(
            "meter.tail.input.unavailable",
            locale=locale,
            input_tokens=_fmt_tokens(receipt.total_input),
        )

    if receipt.total_thinking <= 0:
        output_line = _translate(
            "meter.tail.output.only",
            locale=locale,
            output_tokens=_fmt_tokens(receipt.total_output),
        )
    elif receipt.separate_thinking_tokens <= 0:
        output_line = _translate(
            "meter.tail.output.reasoning_included",
            locale=locale,
            output_tokens=_fmt_tokens(receipt.total_output),
            thinking_tokens=_fmt_tokens(receipt.total_thinking),
        )
    else:
        output_line = _translate(
            "meter.tail.output.reasoning_separate",
            locale=locale,
            output_tokens=_fmt_tokens(receipt.total_output),
            thinking_tokens=_fmt_tokens(receipt.total_thinking),
        )

    no_cache_cost = receipt.no_cache_cost_usd
    cache_savings = receipt.cache_savings_usd
    savings_percent = receipt.cache_savings_percent
    if (
        no_cache_cost is not None
        and cache_savings is not None
        and savings_percent is not None
    ):
        request_line = _translate(
            "meter.tail.requests.savings",
            locale=locale,
            no_cache_cost=_fmt_cost(no_cache_cost, locale=locale),
            cache_savings=_fmt_cost(cache_savings, locale=locale),
            cache_savings_percent=f"{savings_percent:.1f}",
        )
    elif no_cache_cost is not None:
        request_line = _translate(
            "meter.tail.requests.no_cache_only",
            locale=locale,
            no_cache_cost=_fmt_cost(no_cache_cost, locale=locale),
        )
    else:
        request_line = _translate(
            "meter.tail.requests.only",
            locale=locale,
        )
    usage_line = f"{input_line} {output_line}"
    timing_lines = _format_timing_lines(
        total_elapsed_s=total_elapsed_s,
        stage_timings_s=stage_timings_s,
        locale=locale,
    )
    return "\n".join((cost_line, *timing_lines, usage_line, request_line))


def format_cost_tail(
    receipt: UsageReceipt,
    *,
    label: str | None = None,
    locale: str | None = None,
    task_total_usd: float | None = None,
    total_elapsed_s: float | None = None,
    stage_timings_s: Mapping[str, float] | None = None,
) -> str:
    """Deterministically render a cost tail from a receipt (no model call)."""
    return _format_receipt_lines(
        receipt,
        icon="💰",
        label_key="meter.tail.label.foreground",
        label=label,
        locale=locale,
        task_total_usd=task_total_usd,
        total_elapsed_s=total_elapsed_s,
        stage_timings_s=stage_timings_s,
    )


def format_meditation_cost_tail(
    receipt: UsageReceipt,
    *,
    locale: str | None = None,
    task_total_usd: float | None = None,
) -> str:
    """Deterministically render the asynchronous Meditation cost tail.

    Distinct from :func:`format_cost_tail` only in its fixed 🧘 label; the
    Meditation stage finishes after the foreground answer and is never folded
    into the foreground tail, so it gets its own short, model-free message.
    """
    return _format_receipt_lines(
        receipt,
        icon="🧘",
        label_key="meter.tail.label.meditation",
        label=None,
        locale=locale,
        task_total_usd=task_total_usd,
        total_elapsed_s=None,
        stage_timings_s=None,
    )
