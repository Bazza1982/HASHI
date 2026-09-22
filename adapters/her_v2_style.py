"""JEV style decision + existing HER light-model text renderer, opt-in only."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from typing import Any, Mapping

import httpx

from adapters.base import BackendResponse, TokenUsage
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.final_style import FinalStylePass, STYLE_QUESTION
from orchestrator.her_v2.models import Route
from orchestrator.her_v2.progress import ProviderActivityTracker
from orchestrator.her_v2.prompt_catalog import load_prompt_asset
from orchestrator.pcm import load_pcm_document
from orchestrator.privacy_levels import require_level_available

TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
_STYLE_AUTHORITIES = frozenset({"permanent_system", "global_system", "local_system", "persona"})


def capture_style_context(adapter: Any, original_prompt: str, fixed_turn: Any) -> dict[str, Any]:
    """Use typed, accepted PCM sources, never promote history/draft into policy."""
    sources = []
    if fixed_turn is not None:
        coordinator = adapter._session_coordinator
        session = coordinator.store.session(fixed_turn.session_id)
        if not session or session["pcm_revision"] != fixed_turn.pcm_revision:
            raise ValueError("style PCM revision changed")
        for value in (session.get("pcm") or {}).values():
            if isinstance(value, Mapping) and value.get("authority") in _STYLE_AUTHORITIES:
                sources.append({"authority": value["authority"],
                                "source": str(value.get("key") or ""),
                                "text": str(value.get("text") or "")})
        envelope = HerBackendSessionCoordinator.decode(original_prompt) or {}
        current_request = str((envelope.get("turn") or {}).get("user_message") or "")
        if not current_request:
            raise ValueError("style current request unavailable")
    else:
        # Legacy/non-fixed callers have no typed envelope. Read configured sources
        # once at ingress, rather than mining instructions from user-authored text.
        path = getattr(adapter.config, "system_md", None)
        if path:
            document = load_pcm_document(path)
            sources.extend((
                {"authority": "permanent_system", "source": "agent.md#sys", "text": document.system},
                {"authority": "persona", "source": "agent.md#persona", "text": document.persona},
            ))
        runtime = adapter._runtime_context()
        for attr, authority in (("global_sys_prompt_manager", "global_system"),
                                ("sys_prompt_manager", "local_system")):
            manager = getattr(runtime, attr, None)
            if manager is not None:
                for text in manager.get_active_texts():
                    sources.append({"authority": authority, "source": attr, "text": str(text)})
        current_request = original_prompt
    return {"instruction_sources": sources, "current_request": current_request}


def make_final_style_pass(*, provider: Any, config: Any, context: Mapping[str, Any],
                          request_id: str) -> FinalStylePass:
    """Bind the normal light slot, never a hard-coded Flash model or Pro fallback."""
    options = config.style_finalisation
    # Direct is contractually the Quick/light slot. Remove the request-local
    # native voice overlay and use a non-thinking, tool-free presentation call.
    quick = replace(config, voice_origin_active=False).profile_for_route(Route.DIRECT)
    quick = replace(quick, reasoning="off", options={
        **dict(quick.options), "provider_reasoning": "off", "reasoning_effort": "off",
    })
    request_ref = f"hashi-request:{request_id}"

    def observe(event: str, payload: Mapping[str, Any], turn_id: str) -> None:
        if provider.audit_log is not None:
            provider.audit_log.append(
                event_id=f"{turn_id}:style:{event}", turn_id=turn_id,
                request_ref=request_ref, stage="style_finalisation", role="style_editor",
                event=f"style_{event}", payload=dict(payload),
            )

    async def check(state: Mapping[str, Any], turn_id: str) -> Mapping[str, Any]:
        require_level_available(provider.backend_manager.privacy_level)
        api_key = os.environ.get(options.api_key_env, "").strip()
        if not api_key:
            secrets = getattr(provider.backend_manager, "secrets", None)
            if isinstance(secrets, Mapping):
                stored_key = secrets.get(options.api_key_secret)
                if isinstance(stored_key, str):
                    api_key = stored_key.strip()
        if not api_key:
            raise ValueError("style API credential unavailable")
        call_id = f"{turn_id}:style:jev"
        began = time.perf_counter()
        usage = None
        model = options.model
        status = "failed"
        provider_request_id = call_id
        try:
            async with httpx.AsyncClient(timeout=options.check_timeout_s, follow_redirects=False) as client:
                response = await client.post(
                    TYPESAFE_SYSTEM_ONE_URL,
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                    json={"model": options.model, "state": dict(state), "questions": {"style": STYLE_QUESTION}},
                )
            provider_request_id = response.headers.get("x-typesafe-request-id") or call_id
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("style response must be an object")
            model = str(data.get("model") or model)
            raw_usage = data.get("usage") or {}
            if isinstance(raw_usage, dict) and all(
                isinstance(raw_usage.get(key), int) and not isinstance(raw_usage[key], bool)
                and raw_usage[key] >= 0 for key in ("input_tokens", "output_tokens")
            ):
                usage = TokenUsage(input_tokens=raw_usage["input_tokens"],
                                   output_tokens=raw_usage["output_tokens"])
            status = "completed"
            return data
        finally:
            # No invented price or token counts. Preserve one separate physical
            # call receipt, including failures, through the existing meter.
            elapsed = (time.perf_counter() - began) * 1000
            result = BackendResponse(
                text="", is_success=status == "completed", usage=usage,
                duration_ms=elapsed,
                stream_metadata={"meter": {"provider_calls": [{
                    "provider_request_id": provider_request_id, "model": model,
                    "input": usage.input_tokens if usage else 0,
                    "output": usage.output_tokens if usage else 0,
                    "token_source": "provider" if usage else "unknown",
                    "status": status, "provider_call_latency_ms": elapsed,
                }]}},
            )
            provider._accumulate_usage(usage)
            provider._record_usage_line_item(
                request_id=request_ref, phase="style_check", engine="typesafe-api",
                model=model, response=result, invocation_id=call_id,
            )

    async def rewrite(state: Mapping[str, Any], turn_id: str) -> str:
        call_id = f"{turn_id}:style:rewrite"
        provider.bind_persona_audit_context(call_id, turn_id=turn_id, request_ref=request_ref)
        return await provider._package_persona_text_once(
            quick, prompt=json.dumps(dict(state), ensure_ascii=False),
            system_prompt=load_prompt_asset("system_style_rewrite"),
            request_id=call_id, message_label="final style", max_chars=64_000,
            attempt=1, activity=ProviderActivityTracker(), metering_phase="style_rewrite",
        )

    observe_context = hashlib.sha256(
        json.dumps(dict(context), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    def with_context(event: str, payload: Mapping[str, Any], turn_id: str) -> None:
        observe(event, {**payload, "instruction_snapshot_sha256": observe_context,
                        "rewrite_provider": quick.engine, "rewrite_model": quick.model}, turn_id)

    return FinalStylePass(config=options, context=context, check=check, rewrite=rewrite,
                          observe=with_context)
