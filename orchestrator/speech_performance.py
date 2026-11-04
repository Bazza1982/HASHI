"""Transient automatic reading direction. VoiceManager remains the settings owner."""
from __future__ import annotations

import json

from orchestrator import ui_language


def checked_plan(value, model):
    if (not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False)) > 4096
            or value.get("version") != 1 or value.get("model") != model
            or type(value.get("applied")) is not bool
            or not isinstance(value.get("instructions"), str)
            or len(value["instructions"]) > 500
            or (value["applied"] and not value["instructions"].strip())
            or (not value["applied"] and value["instructions"])):
        raise ValueError("Invalid automatic speech performance plan")
    return dict(value)


async def prepare(manager, text, build_provider, *, state=None):
    state = manager._load() if state is None else state
    if state.get("performance") is not True and not (state.get("manual_direction") or {}).get("enabled"):
        return {"enabled": False}
    manager._require_voice_catalog()
    selected = state["provider"]
    service = manager.voice_catalog.service(selected)
    context = {"enabled": True, "provider": selected, "plan": None, "reason": "unsupported"}
    manual=state.get("manual_direction") or {}
    if manual.get("enabled"):
        from orchestrator.manual_speech_direction import plan
        if service.get("adapter") != "openai_compatible" or (service.get("automatic_performance") is False or service.get("location") not in {"local","lan"}):
            raise ValueError(ui_language.tr("voice.manual.unsupported"))
        context.update(plan=plan(manual, service["model"]), reason=None, source="manual")
        return context
    if (service.get("automatic_performance") is False
            or service.get("adapter") != "openai_compatible"
            or service.get("location") not in {"local", "lan"}):
        return context
    if ((state.get("provider_options") or {}).get("instructions")
            or (service.get("request_options") or {}).get("instructions")):
        context["reason"] = "manual_instructions"
        return context
    try:
        provider = build_provider(selected, ffmpeg_cmd=manager.ffmpeg_cmd, provider_config=service,
            secrets=manager._secrets, bridge_home=manager.project_root)
        context["plan"] = checked_plan(await provider.prepare_performance(text), service["model"])
        context["reason"] = context["plan"].get("reason")
    except Exception:
        # CancelledError propagates. A failed plan is explicit and reused.
        context["reason"] = "planner_unavailable"
    return context


def checked_context(value):
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("Invalid speech performance context")
    if not value["enabled"]:
        return {"enabled": False}
    if (not isinstance(value.get("provider"), str) or len(value["provider"]) > 64
            or len(json.dumps(value, ensure_ascii=False)) > 5000):
        raise ValueError("Invalid speech performance context")
    plan = value.get("plan")
    if plan is not None:
        checked_plan(plan, plan.get("model") if isinstance(plan, dict) else None)
    return dict(value)


def options_for(context, provider, options):
    result = dict(options or {})
    if context and context.get("enabled") and provider == context.get("provider") and context.get("plan"):
        if context.get("source") == "manual":
            result.pop("instructions", None)
            result.update(performance=True, performance_plan=context["plan"])
            return result
        # Explicit manual instructions retain precedence over automatic direction.
        if not result.get("instructions"):
            result.update(performance=True, performance_plan=context["plan"])
    return result


def metadata(context, provider):
    if not context or not context.get("enabled"):
        return {"requested": False, "applied": False}
    plan = context.get("plan") or {}
    applied = plan.get("applied") is True and provider == context.get("provider")
    return {"requested": True, "applied": applied, "plan_id": plan.get("plan_id"),
            "reason": None if applied else context.get("reason") or "provider_changed",
            "source": context.get("source", "automatic"), "planning_ms": plan.get("planning_ms"), "sampled": plan.get("sampled", False)}


def notice(performance, existing, *, locale=None):
    if performance.get("requested") and not performance.get("applied"):
        return " ".join(filter(None, (existing, ui_language.tr("voice.performance.unavailable", locale=locale))))
    return existing
