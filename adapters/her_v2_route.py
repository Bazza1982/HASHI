"""TypeSafe/Jev Choice adapter for the HER v2 route boundary."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Mapping

import httpx

from adapters.base import BackendResponse, TokenUsage
from orchestrator.her_v2.route_judgment import (
    ROUTE_QUESTION,
    RouteJudgment,
    RouteJudgmentConfig,
    parse_route_answer,
)

TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"


class TypeSafeRouteJudge:
    """One physical TypeSafe Choice call per HER turn.

    Failures are deliberately surfaced to the runtime, which records a
    degraded/fallback decision and lets the existing Strategy model continue.
    This keeps JEV optional while its typed route is being piloted.
    """

    def __init__(self, *, provider: Any, config: RouteJudgmentConfig) -> None:
        self.provider = provider
        self.config = config

    def _api_key(self) -> str:
        import os

        value = os.environ.get(self.config.api_key_env, "").strip()
        if not value:
            manager = getattr(self.provider, "backend_manager", None)
            secrets = getattr(manager, "secrets", None)
            if isinstance(secrets, Mapping):
                stored = secrets.get(self.config.api_key_secret)
                if isinstance(stored, str):
                    value = stored.strip()
        if not value:
            raise ValueError("route judgment API credential unavailable")
        return value

    async def judge(self, state: Mapping[str, Any], turn_id: str) -> RouteJudgment:
        api_key = self._api_key()
        call_id = f"{turn_id}:route:jev"
        began = time.perf_counter()
        usage: TokenUsage | None = None
        model = self.config.model
        status = "failed"
        provider_request_id = call_id
        try:
            async with httpx.AsyncClient(
                timeout=self.config.check_timeout_s,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    TYPESAFE_SYSTEM_ONE_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Accept": "application/json",
                    },
                    json={
                        "model": self.config.model,
                        "state": dict(state),
                        "questions": {"route": ROUTE_QUESTION},
                    },
                )
            provider_request_id = (
                response.headers.get("x-typesafe-request-id") or call_id
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, Mapping):
                raise ValueError("route judgment response must be an object")
            model = str(data.get("model") or model)
            raw_usage = data.get("usage") or {}
            if isinstance(raw_usage, Mapping) and all(
                isinstance(raw_usage.get(key), int)
                and not isinstance(raw_usage[key], bool)
                and raw_usage[key] >= 0
                for key in ("input_tokens", "output_tokens")
            ):
                usage = TokenUsage(
                    input_tokens=raw_usage["input_tokens"],
                    output_tokens=raw_usage["output_tokens"],
                )
            answer = parse_route_answer(data)
            status = "completed"
            return replace(
                answer,
                model=model,
                provider_request_id=provider_request_id,
            )
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
                                "provider_request_id": provider_request_id,
                                "model": model,
                                "input": usage.input_tokens if usage else 0,
                                "output": usage.output_tokens if usage else 0,
                                "cost_usd": 0.0,
                                "token_source": "provider" if usage else "unknown",
                                "status": status,
                                "provider_call_latency_ms": elapsed,
                            }
                        ]
                    }
                },
            )
            accumulate = getattr(self.provider, "_accumulate_usage", None)
            if callable(accumulate):
                accumulate(usage)
            record = getattr(self.provider, "_record_usage_line_item", None)
            if callable(record):
                record(
                    request_id=f"hashi-request:{turn_id}",
                    phase="route_judgment",
                    engine="typesafe-api",
                    model=model,
                    response=result,
                    invocation_id=call_id,
                )
