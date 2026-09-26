"""Shared, tool-free TypeSafe JEV transport for opt-in HER services."""
from __future__ import annotations
import os
import time
from collections.abc import Mapping
from typing import Any
import httpx
from adapters.base import BackendResponse, TokenUsage
from orchestrator.privacy_levels import require_level_available

TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"


async def judge(
    *,
    provider: Any,
    config: Any,
    state: Mapping[str, Any],
    questions: Mapping[str, Any],
    turn_id: str,
    request_ref: str,
    phase: str,
    serial: int = 1,
) -> Mapping[str, Any]:
    require_level_available(provider.backend_manager.privacy_level)
    key = os.environ.get(config.api_key_env, "").strip()
    if not key:
        raise ValueError("JEV credential unavailable")
    began = time.perf_counter()
    call_id = f"{turn_id}:{phase}:{serial}"
    provider_id = call_id
    usage = None
    model = config.model
    status = "failed"
    try:
        async with httpx.AsyncClient(
            timeout=config.check_timeout_s, follow_redirects=False
        ) as client:
            response = await client.post(
                TYPESAFE_SYSTEM_ONE_URL,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                json={"model": config.model, "state": dict(state), "questions": dict(questions)},
            )
        provider_id = response.headers.get("x-typesafe-request-id") or call_id
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, Mapping):
            raise ValueError("JEV response must be an object")
        model = str(result.get("model") or model)
        raw = result.get("usage")
        if isinstance(raw, Mapping) and all(
            isinstance(raw.get(k), int)
            and not isinstance(raw[k], bool)
            and raw[k] >= 0
            for k in ("input_tokens", "output_tokens")
        ):
            usage = TokenUsage(
                input_tokens=raw["input_tokens"], output_tokens=raw["output_tokens"]
            )
        status = "completed"
        return result
    finally:
        elapsed = (time.perf_counter() - began) * 1000
        result = BackendResponse(
            text="",
            is_success=status == "completed",
            usage=usage,
            duration_ms=elapsed,
            stream_metadata={
                "meter": {
                    "provider_calls": [
                        {
                            "provider_request_id": provider_id,
                            "model": model,
                            "input": usage.input_tokens if usage else 0,
                            "output": usage.output_tokens if usage else 0,
                            "token_source": "provider" if usage else "unknown",
                            "status": status,
                            "provider_call_latency_ms": elapsed,
                        }
                    ]
                }
            },
        )
        provider._accumulate_usage(usage)
        provider._record_usage_line_item(
            request_id=request_ref,
            phase=phase,
            engine="typesafe-api",
            model=model,
            response=result,
            invocation_id=call_id,
        )
