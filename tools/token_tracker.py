"""
token_tracker.py — Per-agent token usage tracking and cost estimation.

Estimates input/output tokens from text length (no API call needed).
Records usage to workspaces/<agent>/token_usage.jsonl.
Provides summary stats for /usage and /status full commands.

Estimation formula (industry standard):
  English: ~4 chars per token
  CJK (Chinese/Japanese/Korean): ~1.5 chars per token
  Mixed: weighted blend
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PRICING_REVISION = "2026-08-23.v1"

# ── Pricing table (USD per million tokens) ────────────────────────────────────
# Key: model name as used in agents.json / HASHI config (normalized to lowercase)
# "cached" = per-million price for cached/prompt-cache input tokens.
# Sources: OpenAI API pricing (2026-04), OpenRouter, Anthropic docs.
PRICING: dict[str, dict[str, float]] = {
    # Anthropic  (cached = 10% of input for sonnet/haiku, opus varies)
    "claude-sonnet-4-6":        {"input": 3.00,  "cached": 0.30,  "output": 15.00},
    "claude-opus-4-6":          {"input": 5.00,  "cached": 0.50,  "output": 25.00},
    "claude-haiku-4-5":         {"input": 1.00,  "cached": 0.10,  "output": 5.00},
    "claude-sonnet-4-5":        {"input": 3.00,  "cached": 0.30,  "output": 15.00},
    "claude-opus-4-5":          {"input": 15.00, "cached": 1.50,  "output": 75.00},
    # Google
    "gemini-2.5-pro":           {"input": 1.25,  "output": 10.00},
    "gemini-2.0-flash":         {"input": 0.10,  "output": 0.40},
    "gemini-3.1-pro-preview":   {"input": 2.00,  "output": 12.00, "thinking": 12.00},
    "gemini-2.5-flash-preview": {"input": 0.15,  "output": 0.60},
    # DeepSeek
    "deepseek-v4-flash":        {"input": 0.14,  "cached": 0.0028,   "output": 0.28},
    "deepseek-v4-flash-vision-exp": {"input": 0.14, "cached": 0.0028, "output": 0.28},
    "deepseek-v4-pro":          {"input": 0.435, "cached": 0.003625, "output": 0.87},
    # Retain historical prices for already-recorded legacy model IDs.
    "deepseek-chat":            {"input": 0.32,  "cached": 0.032, "output": 0.89},
    "deepseek-r1":              {"input": 0.70,  "cached": 0.07,  "output": 2.50},
    # OpenRouter /api/v1/models (2026-08-22). Qwen has prompt-length tiers
    # below; these rows are the base rates for prompts shorter than 32K.
    "qwen/qwen3.7-flash":       {"input": 0.03,  "cached": 0.006, "output": 0.13},
    "z-ai/glm-5.3":             {"input": 1.40,  "cached": 0.26,  "output": 4.40},
    # OpenAI  (gpt-5.x cached = 10% of input)
    "gpt-4o":                   {"input": 2.50,  "cached": 1.25,  "output": 10.00},
    "gpt-4o-mini":              {"input": 0.15,  "cached": 0.075, "output": 0.60},
    "gpt-5.1-codex-mini":       {"input": 0.25,  "cached": 0.025, "output": 2.00},
    "gpt-5.1-codex-max":        {"input": 1.25,  "cached": 0.125, "output": 10.00},
    "gpt-5.2":                  {"input": 1.75,  "cached": 0.175, "output": 14.00},
    "gpt-5.2-codex":            {"input": 1.75,  "cached": 0.175, "output": 14.00},
    "gpt-5.3-codex":            {"input": 1.75,  "cached": 0.175, "output": 14.00},
    "gpt-5.4":                  {"input": 2.50,  "cached": 0.25,  "output": 15.00},
    "gpt-5.4-mini":             {"input": 0.75,  "cached": 0.075, "output": 4.50},
    # OpenRouter (2026-08-23). GPT-5.6 uses higher rates only when an
    # individual provider call exceeds 272K prompt tokens; see PRICING_TIERS.
    "gpt-5.6-luna":             {"input": 0.20,  "cached": 0.02,  "output": 1.20},
    "gpt-5.6-sol":              {"input": 2.00,  "cached": 0.20,  "output": 10.00},
    # CLI fallback (treated as claude-sonnet-4-6 equivalent)
    "default":                  {"input": 3.00,  "cached": 0.30,  "output": 15.00},
}

# Some OpenRouter models change price according to the prompt length of each
# individual provider call. Entries are ordered by ascending inclusive
# ``min_input_tokens``; the last matching tier wins.
PRICING_TIERS: dict[str, tuple[tuple[int, dict[str, float]], ...]] = {
    "qwen/qwen3.7-flash": (
        (32_000, {"input": 0.10, "cached": 0.02, "output": 0.40}),
        (256_000, {"input": 0.20, "cached": 0.04, "output": 0.80}),
    ),
    "gpt-5.6-luna": (
        (272_001, {"input": 0.40, "cached": 0.04, "output": 1.80}),
    ),
    "gpt-5.6-sol": (
        (272_001, {"input": 4.00, "cached": 0.40, "output": 15.00}),
    ),
}

# Characters that are CJK (each ~0.67 tokens vs 0.25 for ASCII)
_CJK_PATTERN = re.compile(
    r'[\u4e00-\u9fff'      # CJK Unified Ideographs
    r'\u3040-\u309f'       # Hiragana
    r'\u30a0-\u30ff'       # Katakana
    r'\uac00-\ud7af]'      # Korean Hangul
)


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using character-based heuristic."""
    if not text:
        return 0
    cjk_chars = len(_CJK_PATTERN.findall(text))
    other_chars = len(text) - cjk_chars
    # CJK: ~1.5 chars/token → 0.667 tokens/char
    # Other: ~4 chars/token → 0.25 tokens/char
    estimated = (cjk_chars * 0.667) + (other_chars * 0.25)
    return max(1, int(estimated))


def _pricing_key(model: str) -> str:
    """Resolve only exact model identifiers or exact provider-qualified slugs.

    The previous bidirectional substring match made partial names such as
    ``gpt-5`` silently inherit the first ``gpt-5.x`` price in dictionary order.
    A provider prefix (for example ``anthropic/claude-sonnet-4-6``) is safe to
    strip only when the remaining basename is itself an exact pricing key.
    """

    model_lower = str(model or "").casefold().strip().strip("/")
    if model_lower in PRICING:
        return model_lower
    if "/" in model_lower:
        basename = model_lower.rsplit("/", 1)[-1]
        if basename in PRICING:
            return basename
    return "default"


def get_price(model: str, *, input_tokens: int | None = None) -> dict[str, float]:
    """Return the applicable per-million-token prices for one provider call."""
    key = _pricing_key(model)
    prices = PRICING[key]
    if input_tokens is None:
        return prices
    for minimum, tier_prices in PRICING_TIERS.get(key, ()):
        if input_tokens >= minimum:
            prices = tier_prices
    return prices


def calc_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
    thinking_tokens: int = 0,
    cached_tokens: int = 0,
    *,
    thinking_in_output: bool = False,
) -> float:
    """Calculate cost in USD.  *cached_tokens* are the portion of
    *input_tokens* that hit prompt cache (charged at reduced rate)."""
    prices = get_price(model, input_tokens=input_tokens)
    cached = min(cached_tokens, input_tokens)
    non_cached = input_tokens - cached
    cached_price = prices.get("cached", prices["input"] * 0.5)  # fallback 50%
    separately_billed_thinking = 0 if thinking_in_output else thinking_tokens
    cost = (
        non_cached * prices["input"] / 1_000_000 +
        cached * cached_price / 1_000_000 +
        output_tokens * prices["output"] / 1_000_000 +
        separately_billed_thinking
        * prices.get("thinking", prices["output"])
        / 1_000_000
    )
    return round(cost, 6)


# ── Cost provenance helpers (used by tools.meter_cost) ───────────────────────

LOCAL_ENGINE_MARKERS = (
    "ollama",
    "lmstudio",
    "lm studio",
    "llamacpp",
    "llama.cpp",
    "vllm",
    "local",
    "localai",
    "text-generation-webui",
    "koboldcpp",
)


def _is_local_engine(engine: str) -> bool:
    normalized = " ".join(str(engine or "").split()).casefold()
    if not normalized:
        return False
    return any(marker in normalized for marker in LOCAL_ENGINE_MARKERS)


def model_has_pricing(model: str) -> bool:
    """True when *model* resolves to an explicit pricing row (not ``default``)."""
    return bool(str(model or "").strip()) and _pricing_key(model) != "default"


def classify_token_source(usage_is_provider: bool) -> str:
    """Map usage provenance to the display contract token source."""
    return "provider" if usage_is_provider else "estimated"


def resolve_cost_source(
    *,
    cost_usd,
    model,
    engine,
):
    """Resolve a per-call cost and its provenance without lying about precision.

    Returns ``(resolved_cost, cost_source)`` where ``cost_source`` is one of
    ``provider`` / ``pricing_table`` / ``local_zero`` / ``unknown``.  ``None``
    cost means "unknown" and is never conflated with a genuine ``0.0``.
    """
    if cost_usd is not None:
        if _is_local_engine(engine):
            return float(cost_usd), "local_zero"
        return float(cost_usd), "provider"
    if _is_local_engine(engine):
        return 0.0, "local_zero"
    if model_has_pricing(model):
        return None, "pricing_table"
    return None, "unknown"


# ── Storage ───────────────────────────────────────────────────────────────────

def _usage_path(workspace_dir: Path) -> Path:
    return workspace_dir / "token_usage.jsonl"


def _audit_path(workspace_dir: Path) -> Path:
    return workspace_dir / "token_audit.jsonl"


def record_usage(
    workspace_dir: Path,
    model: str,
    backend: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
    session_id: str | None = None,
    cost_usd: float | None = None,
    token_source: str | None = None,
    request_id: str = "",
    parent_request_id: str = "",
    phase: str = "",
    engine: str = "",
    line_items: list | None = None,
):
    """Append a usage record and return a structured :class:`UsageReceipt`.

    ``line_items`` may carry a per-stage HER v2 breakdown.  When omitted, a
    single aggregate line item is derived from the positional token arguments.
    The JSONL write remains a single aggregate record for backwards
    compatibility with :func:`get_summary`.
    """
    from tools.meter_cost import PerCallUsageLineItem, UsageReceipt

    resolved_cost, cost_source = resolve_cost_source(
        cost_usd=cost_usd,
        model=model,
        engine=engine or backend,
    )
    normalized_token_source = str(token_source or "").strip().casefold()
    if normalized_token_source in {"api", "provider"}:
        normalized_token_source = "provider"
    elif normalized_token_source != "estimated":
        # Compatibility callers did not historically pass token provenance.
        # Provider-reported cost is a useful hint, but new call sites pass the
        # explicit source even when provider cost is unavailable.
        normalized_token_source = classify_token_source(cost_usd is not None)
    thinking_in_output = normalized_token_source == "provider"
    if resolved_cost is None and cost_source == "pricing_table":
        resolved_cost = calc_cost(
            input_tokens,
            output_tokens,
            model,
            thinking_tokens,
            thinking_in_output=thinking_in_output,
        )
    if line_items is None:
        line_items = [
            PerCallUsageLineItem(
                request_id=str(request_id or ""),
                parent_request_id=str(parent_request_id or ""),
                phase=str(phase or ""),
                engine=str(engine or backend or ""),
                model=str(model or ""),
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                thinking_tokens=int(thinking_tokens or 0),
                token_source=normalized_token_source,
                thinking_in_output=thinking_in_output,
                cost_usd=resolved_cost,
                cost_source=cost_source,
            )
        ]
    receipt = UsageReceipt(
        request_id=str(request_id or ""),
        parent_request_id=str(parent_request_id or ""),
        line_items=list(line_items),
    )
    # The structured receipt is authoritative for multi-stage/multi-model HER
    # turns.  Repricing the role-configured aggregate can contradict /meter and
    # previously wrote a known non-zero receipt as 0.0 in token_usage.jsonl.
    cost = receipt.cost_usd
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "backend": backend,
        "input": input_tokens,
        "output": output_tokens,
        "thinking": thinking_tokens,
        "cost_usd": cost,
        "cost_known": cost is not None,
        "cost_source": receipt.dominant_cost_source(),
        "total_tokens": receipt.total_tokens,
        "session_id": session_id or "",
    }
    path = _usage_path(workspace_dir)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never break the agent over tracking
    return receipt


def record_audit_event(workspace_dir: Path, record: dict[str, Any]) -> None:
    """Append a structured token-audit event to the agent workspace."""
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    path = _audit_path(workspace_dir)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_records(workspace_dir: Path) -> list[dict]:
    path = _usage_path(workspace_dir)
    if not path.exists():
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return records


def get_summary(
    workspace_dir: Path,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Return usage summary dict.

    Returns:
        {
          "all_time": {"input": N, "output": N, "thinking": N, "cost_usd": N, "requests": N},
          "session":  {"input": N, ...},   # only if session_id provided
          "by_model": {"model_name": {"input": N, ...}},
        }
    """
    records = _load_records(workspace_dir)

    def empty():
        return {
            "input": 0,
            "output": 0,
            "thinking": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "unknown_cost_requests": 0,
            "requests": 0,
        }

    all_time = empty()
    session = empty()
    by_model: dict[str, dict] = {}

    for r in records:
        _add(all_time, r)
        if session_id and r.get("session_id") == session_id:
            _add(session, r)
        model = r.get("model", "unknown")
        if model not in by_model:
            by_model[model] = empty()
        _add(by_model[model], r)

    return {
        "all_time": all_time,
        "session": session if session_id else None,
        "by_model": by_model,
    }


def _add(acc: dict, record: dict) -> None:
    acc["input"] += record.get("input", 0)
    acc["output"] += record.get("output", 0)
    acc["thinking"] += record.get("thinking", 0)
    acc["total_tokens"] = int(acc.get("total_tokens") or 0) + int(
        record.get("total_tokens")
        if record.get("total_tokens") is not None
        else (
            int(record.get("input") or 0)
            + int(record.get("output") or 0)
            + int(record.get("thinking") or 0)
        )
    )
    raw_cost = record.get("cost_usd")
    if raw_cost is None:
        acc["unknown_cost_requests"] = int(
            acc.get("unknown_cost_requests") or 0
        ) + 1
    else:
        acc["cost_usd"] += float(raw_cost)
    acc["requests"] += 1


def format_summary_text(
    summary: dict,
    agent_name: str = "",
    *,
    labels: dict[str, str] | None = None,
) -> str:
    """Format usage summary as human-readable text for /usage command."""
    labels = labels or {}

    def label(key: str, fallback: str) -> str:
        return str(labels.get(key) or fallback)

    title = label("title", "📊 Token Usage")
    lines = [f"<b>{title}{' — ' + agent_name if agent_name else ''}</b>"]

    def fmt_block(block_label: str, data: dict) -> str:
        tokens = int(
            data.get("total_tokens")
            or (data["input"] + data["output"] + data["thinking"])
        )
        cost = data["cost_usd"]
        req = data["requests"]
        thinking_note = (
            f" + {_fmt_tokens(data['thinking'])} {label('thinking', 'thinking')}"
            if data["thinking"] > 0
            else ""
        )
        return (
            f"<b>{block_label}</b>\n"
            f"  {_fmt_tokens(data['input'])} {label('input', 'in')} + "
            f"{_fmt_tokens(data['output'])} {label('output', 'out')}{thinking_note}\n"
            f"  {_fmt_tokens(tokens)} {label('total', 'total')} · "
            f"{label('requests', '{count} requests').format(count=req)} · <b>${cost:.4f}</b>"
        )

    all_t = summary.get("all_time", {})
    sess = summary.get("session")
    by_model = summary.get("by_model", {})

    if all_t.get("requests", 0) == 0:
        lines.append(f"<i>{label('no_record', 'No usage recorded yet.')}</i>")
        return "\n".join(lines)

    lines.append("")
    lines.append(fmt_block(label("all_time", "🗄 All Time"), all_t))

    if sess and sess.get("requests", 0) > 0:
        lines.append("")
        lines.append(fmt_block(label("session", "🔄 This Session"), sess))

    if by_model:
        lines.append("")
        lines.append(f"<b>{label('by_model', 'By Model')}:</b>")
        for model, data in sorted(by_model.items(), key=lambda x: -x[1]["cost_usd"]):
            tokens = data["input"] + data["output"]
            lines.append(
                f"  <code>{model}</code>  {_fmt_tokens(tokens)} "
                f"{label('tokens', 'tokens')}  ${data['cost_usd']:.4f}"
            )

    return "\n".join(lines)


def format_status_line(summary: dict) -> str:
    """One-line usage summary for /status full."""
    all_t = summary.get("all_time", {})
    sess = summary.get("session")
    if all_t.get("requests", 0) == 0:
        return "no data"
    all_tokens = all_t["input"] + all_t["output"]
    parts = [f"all-time {_fmt_tokens(all_tokens)} tokens (${all_t['cost_usd']:.4f})"]
    if sess and sess.get("requests", 0) > 0:
        sess_tokens = sess["input"] + sess["output"]
        parts.append(f"session {_fmt_tokens(sess_tokens)} (${sess['cost_usd']:.4f})")
    return " · ".join(parts)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# Public alias so cmd_token can import it
fmt_tokens = _fmt_tokens


def _week_start_utc() -> datetime:
    """Most recent Sunday at 00:00 UTC."""
    now = datetime.now(timezone.utc)
    # weekday(): Mon=0 … Sun=6 → days since last Sunday
    days_since_sunday = (now.weekday() + 1) % 7
    return (now - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _month_start_utc() -> datetime:
    """1st of current month at 00:00 UTC."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def get_summary_extended(
    workspace_dir: Path,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Return usage summary with all_time, session, weekly, monthly, by_model, since."""
    records = _load_records(workspace_dir)

    def empty() -> dict:
        return {
            "input": 0,
            "output": 0,
            "thinking": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "unknown_cost_requests": 0,
            "requests": 0,
        }

    all_time = empty()
    session  = empty()
    weekly   = empty()
    monthly  = empty()
    by_model: dict[str, dict] = {}

    week_start  = _week_start_utc()
    month_start = _month_start_utc()

    for r in records:
        _add(all_time, r)
        if session_id and r.get("session_id") == session_id:
            _add(session, r)
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= week_start:
                _add(weekly, r)
            if ts >= month_start:
                _add(monthly, r)
        except Exception:
            pass
        model = r.get("model", "unknown")
        if model not in by_model:
            by_model[model] = empty()
        _add(by_model[model], r)

    earliest = records[0].get("ts", "")[:10] if records else None

    return {
        "all_time": all_time,
        "session":  session if session_id else None,
        "weekly":   weekly,
        "monthly":  monthly,
        "by_model": by_model,
        "since":    earliest,
    }
