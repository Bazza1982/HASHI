"""HERV3 runtime-report rendering under a retained compatibility module name.

The ``herv2_card`` import path and metadata readers remain stable while public
copy describes HERV3. The formatter exposes model reasoning, optional Strategy
Cards, model usage, and terminal state; it never recreates retired route or
cognitive-stage presentation.
"""

from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Herv2StageItem:
    """One legacy-compatible usage item within a HERV3 Turn."""

    stage: str
    slot: str  # "Quick", "Pro", "Custom", or "n/a"
    model: str
    engine: str = ""
    elapsed_s: float | None = None
    tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class Herv2CardData:
    """Structured facts extracted from HERV3 compatibility metadata."""

    turn_id: str
    classification: str
    terminal_state: str
    strategy_cards: tuple[str, ...] = ()
    strategy_card_details: tuple[Mapping[str, str], ...] = ()
    execution_brief: str = ""
    effort: str = ""
    stages: tuple[Herv2StageItem, ...] = ()
    review_count: int = 0
    replan_count: int = 0
    checkpoint_count: int = 0
    execution_route: str = ""


# Human-friendly stage labels (zh-CN and en)
_STAGE_NAMES: dict[str, dict[str, str]] = {
    "triage": {"zh": "分诊", "en": "Triage"},
    "planning": {"zh": "规划", "en": "Planning"},
    "replanning": {"zh": "重规划", "en": "Replanning"},
    "execution": {"zh": "执行", "en": "Execution"},
    "review": {"zh": "评审", "en": "Review"},
    "direct": {"zh": "直答", "en": "Direct"},
    "immediate_response": {"zh": "即时响应", "en": "Immediate"},
    "persona": {"zh": "人设润色", "en": "Persona"},
    "finalisation": {"zh": "收尾", "en": "Finalisation"},
}

# Resolved model-slot display labels (zh-CN and en)
_SLOT_DISPLAY: dict[str, dict[str, str]] = {
    "Quick": {"zh": "快速档", "en": "Quick"},
    "Pro": {"zh": "专业档", "en": "Pro"},
}


def _is_zh(locale: str | None) -> bool:
    loc = str(locale or "").casefold()
    return "zh" in loc or "cn" in loc


def _fmt_duration(seconds: float | None, *, is_zh: bool) -> str:
    if seconds is None:
        return ""
    try:
        val = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(val) or val < 0:
        return ""
    if val < 0.05:
        return "<0.1秒" if is_zh else "<0.1s"
    if val < 60:
        formatted = f"{val:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}秒" if is_zh else f"{formatted}s"
    minutes = int(val // 60)
    remaining_s = int(round(val % 60))
    return f"{minutes}分{remaining_s}秒" if is_zh else f"{minutes}m {remaining_s}s"


def _fmt_number(value: float) -> str:
    """One-decimal compact number without a trailing zero, e.g. 65.3."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _fmt_tokens(count: int, *, is_zh: bool) -> str:
    if count <= 0:
        return ""
    if is_zh:
        if count >= 10000:
            return f"{_fmt_number(count / 10000.0)}万 token"
        return f"{count:,} token"
    if count >= 1000:
        return f"{_fmt_number(count / 1000.0)}K tokens"
    return f"{count:,} tokens"


def _slot_display(slot: str, *, is_zh: bool) -> str:
    return _SLOT_DISPLAY.get(slot, {}).get("zh" if is_zh else "en", slot)


def _resolve_slot(
    stage_name: str,
    model: str,
    *,
    slot_models: Mapping[str, str] | None = None,
    route_model_slots: Mapping[str, str] | None = None,
) -> str:
    """Determine whether a stage used Quick (fast) or Pro model slot."""
    norm_stage = stage_name.strip().lower()
    norm_model = model.strip().lower()

    # Direct is unconditionally Quick
    if norm_stage == "direct":
        return "Quick"

    # Check slot_models mapping
    if slot_models:
        fast_target = str(slot_models.get("fast") or slot_models.get("quick") or "").strip().lower()
        pro_target = str(slot_models.get("pro") or "").strip().lower()
        if norm_model and norm_model == fast_target:
            return "Quick"
        if norm_model and norm_model == pro_target:
            return "Pro"

    # Check route_model_slots mapping
    route_key = {
        "triage": "triage",
        "planning": "plan",
        "replanning": "plan",
        "execution": "execute",
        "review": "review",
        "direct": "direct",
    }.get(norm_stage)
    if route_model_slots and route_key and route_key in route_model_slots:
        slot_val = str(route_model_slots[route_key]).strip().lower()
        return "Quick" if slot_val in {"fast", "quick"} else "Pro"

    # Conventional defaults
    if norm_stage in {"triage", "direct"}:
        return "Quick"
    if norm_stage in {"planning", "replanning", "execution", "review"}:
        return "Pro"

    # Model name heuristic
    if any(q in norm_model for q in ("flash", "mini", "lite", "small", "haiku", "light")):
        return "Quick"
    if any(p in norm_model for p in ("pro", "sonnet", "opus", "plus", "max", "large")):
        return "Pro"

    return "n/a"


def herv2_card_data_from_metadata(
    her_v2: Mapping[str, Any],
    *,
    meter: Mapping[str, Any] | None = None,
    stage_timings_s: Mapping[str, float] | None = None,
) -> Herv2CardData | None:
    """Build a Herv2CardData snapshot from response stream metadata."""
    if not isinstance(her_v2, Mapping) or not her_v2:
        return None

    turn_id = str(her_v2.get("turn_id") or "")
    classification = str(her_v2.get("classification") or "").strip()
    terminal_state = str(her_v2.get("terminal_state") or "COMPLETED")
    execution_route = str(her_v2.get("execution_route") or "").strip().upper()

    # Strategy cards
    raw_cards = her_v2.get("strategy_cards") or ()
    strategy_cards = tuple(str(c) for c in raw_cards if c)

    raw_details = her_v2.get("strategy_card_details") or ()
    strategy_card_details = tuple(
        dict(d) for d in raw_details if isinstance(d, Mapping)
    )

    # Execution brief
    raw_brief = her_v2.get("strategy_brief") or her_v2.get("execution_brief") or ""
    if isinstance(raw_brief, Mapping):
        execution_brief = str(
            raw_brief.get("summary") or raw_brief.get("core_objective") or ""
        )
    else:
        execution_brief = str(raw_brief or "")

    # Effort
    raw_effort = her_v2.get("effort")
    if isinstance(raw_effort, Mapping):
        effort = str(
            raw_effort.get("effective")
            or raw_effort.get("requested_effort")
            or raw_effort.get("canonical_effort")
            or ""
        )
    else:
        effort = str(raw_effort or "")

    # Older HER metadata did not carry an explicit execution route.  The
    # effective zero policy is authoritative for the Direct path, so retain a
    # truthful card value while those responses are still in circulation.
    if not classification and not execution_route and effort.casefold() == "zero":
        execution_route = "DIRECT"

    # Stage timings
    timings = dict(stage_timings_s or her_v2.get("stage_timings_s") or {})

    # Slot models / route configurations
    slot_models = her_v2.get("slot_models") if isinstance(her_v2.get("slot_models"), Mapping) else {}
    route_model_slots = her_v2.get("route_model_slots") if isinstance(her_v2.get("route_model_slots"), Mapping) else {}

    # Parse stage invocations from line_items or synthesized from timings
    line_items = []
    if isinstance(meter, Mapping) and isinstance(meter.get("line_items"), Sequence):
        line_items = list(meter["line_items"])

    stages: list[Herv2StageItem] = []
    seen_phases: set[str] = set()

    for item in line_items:
        if not isinstance(item, Mapping):
            continue
        phase = str(item.get("phase") or "").strip().lower()
        if not phase or phase in {"wrapper", "meditation", "dream"}:
            continue
        seen_phases.add(phase)
        model = str(item.get("model") or "")
        engine = str(item.get("engine") or "")
        slot = _resolve_slot(phase, model, slot_models=slot_models, route_model_slots=route_model_slots)

        elapsed = None
        if phase in timings:
            elapsed = float(timings[phase])
        elif item.get("provider_call_latency_ms") is not None:
            elapsed = float(item["provider_call_latency_ms"]) / 1000.0

        total_tok = (
            int(item.get("input") or item.get("input_tokens") or 0)
            + int(item.get("output") or item.get("output_tokens") or 0)
            + int(item.get("thinking") or item.get("thinking_tokens") or 0)
        )
        cost = item.get("cost_usd")
        try:
            cost_usd = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost_usd = None

        stages.append(
            Herv2StageItem(
                stage=phase,
                slot=slot,
                model=model,
                engine=engine,
                elapsed_s=elapsed,
                tokens=total_tok,
                cost_usd=cost_usd,
            )
        )

    # If no line items were available but timings exist, synthesize stage items
    if not stages and timings:
        for phase, elapsed_s in timings.items():
            norm_p = str(phase).strip().lower()
            if norm_p in {"wrapper", "meditation", "dream"}:
                continue
            slot = _resolve_slot(norm_p, "", slot_models=slot_models, route_model_slots=route_model_slots)
            stages.append(
                Herv2StageItem(
                    stage=norm_p,
                    slot=slot,
                    model="",
                    elapsed_s=float(elapsed_s),
                )
            )

    return Herv2CardData(
        turn_id=turn_id,
        classification=classification,
        terminal_state=terminal_state,
        execution_route=execution_route,
        strategy_cards=strategy_cards,
        strategy_card_details=strategy_card_details,
        execution_brief=execution_brief,
        effort=effort,
        stages=tuple(stages),
        review_count=int(her_v2.get("review_count") or 0),
        replan_count=int(her_v2.get("replan_count") or 0),
        checkpoint_count=int(her_v2.get("checkpoint_count") or 0),
    )


def _aggregate_stages(stages: Sequence[Herv2StageItem]) -> list[dict[str, Any]]:
    """Collapse repeated (stage, slot, model) invocations into single rows."""
    order: list[tuple[str, str, str]] = []
    groups: dict[tuple[str, str, str], list[Herv2StageItem]] = {}
    for st in stages:
        key = (st.stage, st.slot, st.model)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(st)

    rows: list[dict[str, Any]] = []
    for key in order:
        items = groups[key]
        elapsed_vals = [it.elapsed_s for it in items if it.elapsed_s is not None]
        rows.append(
            {
                "stage": key[0],
                "slot": key[1],
                "model": key[2],
                "rounds": len(items),
                "elapsed_s": sum(elapsed_vals) if elapsed_vals else None,
                "tokens": sum(it.tokens for it in items),
            }
        )
    return rows


def _stage_parts(row: Mapping[str, Any], *, is_zh: bool) -> dict[str, str]:
    """Localized display components for one aggregated stage row."""
    stage_name = str(row["stage"])
    slot = str(row["slot"])
    model = str(row["model"] or "")
    rounds = int(row["rounds"])
    elapsed = row["elapsed_s"]

    stage_disp = _STAGE_NAMES.get(stage_name, {}).get(
        "zh" if is_zh else "en"
    ) or stage_name.capitalize()
    slot_disp = _slot_display(slot, is_zh=is_zh)

    parts: dict[str, str] = {
        "stage": stage_disp,
        "slot": slot_disp,
        "model": model,
        "rounds": "",
        "duration": "",
        "tokens": "",
    }

    if rounds > 1:
        parts["rounds"] = f"{rounds}轮" if is_zh else f"{rounds} rounds"

    dur = _fmt_duration(elapsed, is_zh=is_zh)
    if dur:
        if rounds > 1:
            dur = f"合计{dur}" if is_zh else f"Total {dur}"
        parts["duration"] = dur

    tok = _fmt_tokens(int(row["tokens"]), is_zh=is_zh)
    if tok:
        parts["tokens"] = tok

    return parts


def _finalisation_text(has_finalisation: bool, *, is_zh: bool) -> str:
    if has_finalisation:
        return "已做合并总结" if is_zh else "Merged summary produced"
    return (
        "未做合并总结（本档由执行直接给出最终答复）"
        if is_zh
        else "No merge summary (this tier delivered the final answer directly via execution)"
    )


def format_herv2_card(
    data: Herv2CardData,
    *,
    locale: str | None = "zh-CN",
    surface: str = "telegram",
) -> str:
    """Format a HERV3 runtime report for Telegram or plain-text surfaces."""
    is_zh = _is_zh(locale)
    is_tg = surface == "telegram"
    title_text = "HERV3 运行报告" if is_zh else "HERV3 Runtime Report"
    title = f"🧭 <b>{title_text}</b>" if is_tg else f"🧭 {title_text}"
    label_sep = "：" if is_zh else ": "
    reasoning_label = "模型推理" if is_zh else "Model reasoning"
    cards_label = "策略卡（可选参考）" if is_zh else "Strategy Cards (optional)"
    models_label = "模型调用" if is_zh else "Model calls"
    state_label = "最终状态" if is_zh else "State"

    lines: list[str] = [title, "" if is_tg else "─" * 40]
    reasoning = data.effort or ("未报告" if is_zh else "not reported")
    if is_tg:
        lines.append(
            f"<b>{reasoning_label}{label_sep}</b><code>{html.escape(reasoning)}</code>"
        )
    else:
        lines.append(f"{reasoning_label}{label_sep}{reasoning}")

    if data.strategy_card_details:
        titles = [
            str(card.get("title") or "").strip()
            for card in data.strategy_card_details
            if str(card.get("title") or "").strip()
        ]
        cards_text = " · ".join(titles)
    elif data.strategy_cards:
        cards_text = " · ".join(str(card) for card in data.strategy_cards)
    else:
        cards_text = "无" if is_zh else "None"
    if is_tg:
        lines.append(f"<b>{cards_label}{label_sep}</b>{html.escape(cards_text)}")
    else:
        lines.append(f"{cards_label}{label_sep}{cards_text}")

    model_rows: dict[tuple[str, str], dict[str, float | int]] = {}
    for item in data.stages:
        key = (item.engine, item.model)
        row = model_rows.setdefault(key, {"calls": 0, "elapsed_s": 0.0, "tokens": 0})
        row["calls"] = int(row["calls"]) + 1
        row["elapsed_s"] = float(row["elapsed_s"]) + float(item.elapsed_s or 0.0)
        row["tokens"] = int(row["tokens"]) + int(item.tokens or 0)
    if model_rows:
        lines.append("")
        lines.append(f"<b>{models_label}{label_sep}</b>" if is_tg else f"{models_label}{label_sep}")
        for (engine, model), row in model_rows.items():
            target = " / ".join(part for part in (engine, model) if part) or "unknown"
            calls = int(row["calls"])
            segments = [target]
            if calls > 1:
                segments.append(f"{calls} 次" if is_zh else f"{calls} calls")
            duration = _fmt_duration(float(row["elapsed_s"]), is_zh=is_zh)
            if duration:
                segments.append(duration)
            tokens = _fmt_tokens(int(row["tokens"]), is_zh=is_zh)
            if tokens:
                segments.append(tokens)
            if is_tg:
                escaped = [html.escape(segment) for segment in segments]
                escaped[0] = f"<code>{escaped[0]}</code>"
                lines.append("• " + " · ".join(escaped))
            else:
                lines.append("• " + " · ".join(segments))

    lines.append("")
    state = data.terminal_state or "UNKNOWN"
    if is_tg:
        lines.append(f"<b>{state_label}{label_sep}</b><code>{html.escape(state)}</code>")
    else:
        lines.append(f"{state_label}{label_sep}{state}")
    return "\n".join(lines)
