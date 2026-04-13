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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Pricing table (USD per million tokens, from OpenRouter) ──────────────────
# Key: model name as used in agents.json / HASHI config (normalized to lowercase)
PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4-6":        {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":          {"input": 5.00,  "output": 25.00},   # OpenRouter: $5/$25 (Amazon Bedrock)
    "claude-haiku-4-5":         {"input": 1.00,  "output": 5.00},
    "claude-sonnet-4-5":        {"input": 3.00,  "output": 15.00},
    "claude-opus-4-5":          {"input": 15.00, "output": 75.00},
    # Google
    "gemini-2.5-pro":           {"input": 1.25,  "output": 10.00},
    "gemini-2.0-flash":         {"input": 0.10,  "output": 0.40},
    "gemini-3.1-pro-preview":   {"input": 2.00,  "output": 12.00, "thinking": 12.00},
    "gemini-2.5-flash-preview": {"input": 0.15,  "output": 0.60},
    # DeepSeek
    "deepseek-chat":            {"input": 0.32,  "output": 0.89},
    "deepseek-r1":              {"input": 0.70,  "output": 2.50},
    # OpenAI
    "gpt-4o":                   {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":              {"input": 0.15,  "output": 0.60},
    "gpt-5.1-codex-mini":       {"input": 0.25,  "output": 2.00},
    "gpt-5.1-codex-max":        {"input": 1.25,  "output": 10.00},
    "gpt-5.2":                  {"input": 1.75,  "output": 14.00},
    "gpt-5.2-codex":            {"input": 1.75,  "output": 14.00},
    "gpt-5.3-codex":            {"input": 1.75,  "output": 14.00},
    "gpt-5.4":                  {"input": 2.50,  "output": 15.00},
    # CLI fallback (treated as claude-sonnet-4-6 equivalent)
    "default":                  {"input": 3.00,  "output": 15.00},
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


def get_price(model: str) -> dict[str, float]:
    """Return pricing dict for a model, falling back to default."""
    model_lower = model.lower().strip()
    # Direct match
    if model_lower in PRICING:
        return PRICING[model_lower]
    # Partial match (e.g. "claude-sonnet-4-6" in "anthropic/claude-sonnet-4-6")
    for key, prices in PRICING.items():
        if key in model_lower or model_lower in key:
            return prices
    return PRICING["default"]


def calc_cost(input_tokens: int, output_tokens: int, model: str,
              thinking_tokens: int = 0) -> float:
    """Calculate cost in USD."""
    prices = get_price(model)
    cost = (
        input_tokens * prices["input"] / 1_000_000 +
        output_tokens * prices["output"] / 1_000_000 +
        thinking_tokens * prices.get("thinking", prices["output"]) / 1_000_000
    )
    return round(cost, 6)


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
) -> None:
    """Append a usage record to the agent's token_usage.jsonl."""
    cost = calc_cost(input_tokens, output_tokens, model, thinking_tokens)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "backend": backend,
        "input": input_tokens,
        "output": output_tokens,
        "thinking": thinking_tokens,
        "cost_usd": cost,
        "session_id": session_id or "",
    }
    path = _usage_path(workspace_dir)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never break the agent over tracking


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
        return {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}

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
    acc["cost_usd"] += record.get("cost_usd", 0.0)
    acc["requests"] += 1


def format_summary_text(summary: dict, agent_name: str = "") -> str:
    """Format usage summary as human-readable text for /usage command."""
    lines = [f"<b>📊 Token Usage{' — ' + agent_name if agent_name else ''}</b>"]

    def fmt_block(label: str, data: dict) -> str:
        tokens = data["input"] + data["output"] + data["thinking"]
        cost = data["cost_usd"]
        req = data["requests"]
        thinking_note = f" + {_fmt_tokens(data['thinking'])} thinking" if data["thinking"] > 0 else ""
        return (
            f"<b>{label}</b>\n"
            f"  {_fmt_tokens(data['input'])} in + {_fmt_tokens(data['output'])} out{thinking_note}\n"
            f"  {_fmt_tokens(tokens)} total · {req} requests · <b>${cost:.4f}</b>"
        )

    all_t = summary.get("all_time", {})
    sess = summary.get("session")
    by_model = summary.get("by_model", {})

    if all_t.get("requests", 0) == 0:
        lines.append("<i>No usage recorded yet.</i>")
        return "\n".join(lines)

    lines.append("")
    lines.append(fmt_block("🗄 All Time", all_t))

    if sess and sess.get("requests", 0) > 0:
        lines.append("")
        lines.append(fmt_block("🔄 This Session", sess))

    if by_model:
        lines.append("")
        lines.append("<b>By Model:</b>")
        for model, data in sorted(by_model.items(), key=lambda x: -x[1]["cost_usd"]):
            tokens = data["input"] + data["output"]
            lines.append(
                f"  <code>{model}</code>  {_fmt_tokens(tokens)} tokens  ${data['cost_usd']:.4f}"
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
        return {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}

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
