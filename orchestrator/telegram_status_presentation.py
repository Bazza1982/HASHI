"""Read and render the owner-scoped, bounded Telegram status projection."""
from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from orchestrator import ui_language


async def read_summary(runtime: Any, owner_id: str, *, actor_id: int | None = None) -> dict[str, Any]:
    facade = getattr(runtime, "orchestrator", None)
    reader = getattr(facade, "telegram_summary", None)
    if not callable(reader):
        return {"ok": False}
    try:
        value = await reader(owner_id, actor_id)
    except Exception:
        return {"ok": False}
    return dict(value) if isinstance(value, Mapping) else {"ok": False}


def summary_facts(summary: Mapping[str, Any] | None) -> list[str]:
    if summary is None:
        return []
    if summary.get("ok") is not True:
        return [html.escape(ui_language.tr("menu.telegram.summary_unavailable"))]

    keys = ("configured_verified", "ready", "mirror_active", "not_ready",
            "unverified_bindings", "stale_bindings")
    if (any(type(summary.get(key)) is not int or not 0 <= summary[key] <= 1024 for key in keys)
            or type(summary.get("revision")) is not int or not 0 <= summary["revision"] < 2**63
            or summary["ready"] + summary["not_ready"] != summary["configured_verified"]
            or summary["mirror_active"] > summary["ready"]):
        return [html.escape(ui_language.tr("menu.telegram.summary_unavailable"))]

    def count(key: str) -> int:
        return summary[key]

    instance = str(summary.get("instance") or "").strip()[:80]
    facts = [html.escape(ui_language.tr("menu.telegram.summary_instance", instance=instance))] if instance else []
    facts.append(html.escape(ui_language.tr(
        "menu.telegram.summary_counts",
        configured=count("configured_verified"),
        ready=count("ready"),
        mirror=count("mirror_active"),
        not_ready=count("not_ready"),
    )))
    unknown = count("unverified_bindings")
    if unknown:
        facts.append(html.escape(ui_language.tr("menu.telegram.summary_unknown", count=unknown)))
    stale = count("stale_bindings")
    if stale:
        facts.append(html.escape(ui_language.tr("menu.telegram.summary_stale", count=stale)))
    observed = str(summary.get("observed_at") or "").strip()[:40]
    latest = str(summary.get("latest_inbound_success_at") or "").strip()[:40]
    if observed:
        facts.append(html.escape(ui_language.tr("menu.telegram.summary_observed", observed=observed,
                                                revision=summary["revision"],
                                                latest=latest or ui_language.tr("menu.telegram.summary_never"))))
    reasons = summary.get("failure_reasons")
    if isinstance(reasons, list):
        from orchestrator.telegram_ingress_diagnostics import _MESSAGES as safe_reasons
        labels = [ui_language.tr("menu.telegram.failure." + reason)
                  for reason in reasons if isinstance(reason, str) and reason in safe_reasons]
        if labels:
            facts.append(html.escape(ui_language.tr("menu.telegram.summary_failures", reasons=", ".join(labels))))
    facts.append(html.escape(ui_language.tr("menu.telegram.summary_receipt")))
    return facts
# HASHI-Fingerprint: hfp_0587e96e7f884e8e9a872c67e6ace11c
