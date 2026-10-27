"""Focused PAO tests for typed capacity retry and fallback execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.base import BackendCapabilities, BackendResponse
from orchestrator import model_fallback_policy as policy_owner
from orchestrator.runtime_capacity_recovery import generate_with_capacity_recovery


def _capacity(*, effects: bool = False, retry_after_s: float | None = None):
    return BackendResponse(
        text="",
        duration_ms=10,
        error="Selected model is at capacity. Please try a different model.",
        is_success=False,
        error_code="PROVIDER_CAPACITY_UNAVAILABLE",
        error_retryable=True,
        retry_after_s=retry_after_s,
        side_effects_possible=effects,
        tool_call_count=1 if effects else 0,
    )


def _success(text: str = "done"):
    return BackendResponse(text=text, duration_ms=10, is_success=True)


class _Manager:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.fallback_calls = []
        self.current_backend = SimpleNamespace(
            _session_id="codex-session-1",
            capabilities=BackendCapabilities(
                supports_sessions=True,
                supports_files=True,
                supports_tool_use=True,
                supports_thinking_stream=True,
                supports_headless_mode=True,
            ),
            model="gpt-5-codex",
        )
        self.privacy_level = 1
        self.config = SimpleNamespace(
            active_backend="codex-cli",
            allowed_backends=[
                {
                    "engine": "codex-cli",
                    "model": "gpt-5-codex",
                    "models": ["gpt-5-codex", "gpt-5.1-codex"],
                }
            ],
        )

    async def generate_response(self, prompt, request_id, **kwargs):
        self.calls.append((prompt, request_id, dict(kwargs)))
        return self.responses.pop(0)

    def get_her_v2_provider_options(self):
        return [
            {
                "engine": "deepseek-api",
                "models": ["deepseek-v4-flash"],
                "available": True,
                "estimated_fallback_call_cost_usd": 0.01,
            }
        ]

    async def generate_fallback_response(self, **kwargs):
        self.fallback_calls.append(dict(kwargs))
        return _success("fallback done")


def _runtime(tmp_path, responses):
    manager = _Manager(responses)
    return SimpleNamespace(
        backend_manager=manager,
        config=SimpleNamespace(active_backend="codex-cli"),
        global_config=SimpleNamespace(bridge_home=tmp_path),
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        _log_maintenance=lambda *args, **kwargs: None,
    )


def _item(**overrides):
    values = {
        "request_id": "req-capacity-1",
        "owner_id": "owner-1",
        "source": "scheduler",
        "silent": True,
        "request_mode": "text",
        "request_metadata": {},
        "request_content": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _save(runtime, policy):
    _old, document = policy_owner.read_policy(
        runtime, "owner-1", for_update=True
    )
    policy_owner.write_policy(
        runtime, "owner-1", policy, document=document
    )


@pytest.mark.asyncio
async def test_off_keeps_the_original_single_call(tmp_path):
    runtime = _runtime(tmp_path, [_capacity()])
    waits = []
    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=lambda seconds: waits.append(seconds),
    )
    assert response.error_code == "PROVIDER_CAPACITY_UNAVAILABLE"
    assert len(runtime.backend_manager.calls) == 1
    assert waits == []


@pytest.mark.asyncio
async def test_retry_runs_three_bounded_waits_and_honors_retry_after(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_capacity(retry_after_s=90), _capacity(), _capacity(), _success()],
    )
    _save(runtime, policy_owner.FallbackPolicy(mode=policy_owner.MODE_RETRY))
    waits = []

    async def _sleep(seconds):
        waits.append(seconds)

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=_sleep,
    )
    assert response.text == "done"
    assert waits == [90, 180, 300]
    assert [call[2]["is_retry"] for call in runtime.backend_manager.calls] == [
        False,
        True,
        True,
        True,
    ]
    assert runtime.backend_manager.fallback_calls == []


@pytest.mark.asyncio
async def test_call_text_turn_never_waits_or_falls_back(tmp_path):
    runtime = _runtime(tmp_path, [_capacity() for _ in range(4)])
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
        provider="deepseek-api",
        model="deepseek-v4-flash",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )
    sleeper = AsyncMock()

    response = await generate_with_capacity_recovery(
        runtime,
        _item(request_mode="call"),
        "call transcript text",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=sleeper,
    )

    assert response.error_code == policy_owner.CAPACITY_FAILURE_CODE
    assert len(runtime.backend_manager.calls) == 1
    sleeper.assert_not_awaited()
    assert runtime.backend_manager.fallback_calls == []
    recovery = response.stream_metadata["capacity_recovery"]
    assert recovery["blocking_reason"] == policy_owner.REASON_MODALITY_EXCLUDED
    assert recovery["retry_delays_s"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item_overrides",
    [
        {"request_mode": "phone"},
        {"request_mode": "voice"},
        {"request_mode": "text", "request_metadata": {"voice_origin": True}},
    ],
    ids=["phone-mode", "voice-mode", "voice-origin"],
)
async def test_phone_and_voice_requests_never_enter_long_retry_waits(
    tmp_path, item_overrides
):
    runtime = _runtime(tmp_path, [_capacity() for _ in range(4)])
    _save(runtime, policy_owner.FallbackPolicy(mode=policy_owner.MODE_RETRY))
    sleeper = AsyncMock()

    response = await generate_with_capacity_recovery(
        runtime,
        _item(**item_overrides),
        "live request",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=sleeper,
    )

    assert response.error_code == policy_owner.CAPACITY_FAILURE_CODE
    assert len(runtime.backend_manager.calls) == 1
    sleeper.assert_not_awaited()
    assert runtime.backend_manager.fallback_calls == []
    recovery = response.stream_metadata["capacity_recovery"]
    assert recovery["blocking_reason"] == policy_owner.REASON_MODALITY_EXCLUDED
    assert recovery["retry_delays_s"] == []


@pytest.mark.asyncio
async def test_default_a_pauses_after_possible_effects(tmp_path):
    runtime = _runtime(tmp_path, [_capacity(effects=True)])
    _save(runtime, policy_owner.FallbackPolicy(mode=policy_owner.MODE_RETRY))
    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )
    assert len(runtime.backend_manager.calls) == 1
    recovery = response.stream_metadata["capacity_recovery"]
    assert recovery["blocking_reason"] == policy_owner.REASON_EFFECT_PAUSED
    assert recovery["user_notice_code"] == policy_owner.REASON_EFFECT_PAUSED
    assert "side effects" in response.error
    assert recovery["retry_attempts"] == 0


@pytest.mark.asyncio
async def test_explicit_b_resumes_same_session_without_replaying_prompt(tmp_path):
    runtime = _runtime(tmp_path, [_capacity(effects=True), _success()])
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_RETRY,
            sources={
                policy_owner.SOURCE_CODEX_CLI: policy_owner.FallbackSourceRule(
                    side_effect_mode=policy_owner.EFFECT_RESUME
                ),
                policy_owner.SOURCE_HERV3: policy_owner.FallbackSourceRule(),
            },
        ),
    )
    waits = AsyncMock()
    response = await generate_with_capacity_recovery(
        runtime,
        _item(request_content={"parts": [{"type": "image"}]}),
        "SECRET ORIGINAL PROMPT",
        {
            "is_retry": False,
            "silent": True,
            "on_stream_event": None,
            "request_content": {"parts": [{"type": "image"}]},
        },
        sleep=waits,
    )
    assert response.is_success is True
    retry_prompt, _request_id, retry_kwargs = runtime.backend_manager.calls[1]
    assert "SECRET ORIGINAL PROMPT" not in retry_prompt
    assert "unfinished work" in retry_prompt
    assert "request_content" not in retry_kwargs
    assert retry_kwargs["is_retry"] is True


@pytest.mark.asyncio
async def test_explicit_resume_remains_cumulative_across_later_retries(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_capacity(effects=True), _capacity(), _success()],
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_RETRY,
            sources={
                policy_owner.SOURCE_CODEX_CLI: policy_owner.FallbackSourceRule(
                    side_effect_mode=policy_owner.EFFECT_RESUME
                ),
                policy_owner.SOURCE_HERV3: policy_owner.FallbackSourceRule(),
            },
        ),
    )
    content = {"parts": [{"type": "image", "name": "original.png"}]}

    response = await generate_with_capacity_recovery(
        runtime,
        _item(request_content=content),
        "SECRET ORIGINAL PROMPT",
        {
            "is_retry": False,
            "silent": True,
            "on_stream_event": None,
            "request_content": content,
        },
        sleep=AsyncMock(),
    )

    assert response.is_success is True
    assert len(runtime.backend_manager.calls) == 3
    for retry_prompt, _request_id, retry_kwargs in runtime.backend_manager.calls[1:]:
        assert "SECRET ORIGINAL PROMPT" not in retry_prompt
        assert "unfinished work" in retry_prompt
        assert "request_content" not in retry_kwargs


@pytest.mark.asyncio
async def test_explicit_resume_stops_when_original_session_disappears(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_capacity(effects=True), _capacity(), _success()],
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_RETRY,
            sources={
                policy_owner.SOURCE_CODEX_CLI: policy_owner.FallbackSourceRule(
                    side_effect_mode=policy_owner.EFFECT_RESUME
                ),
                policy_owner.SOURCE_HERV3: policy_owner.FallbackSourceRule(),
            },
        ),
    )
    original_generate = runtime.backend_manager.generate_response

    async def _drop_session_after_first_resume(prompt, request_id, **kwargs):
        response = await original_generate(prompt, request_id, **kwargs)
        if len(runtime.backend_manager.calls) == 2:
            runtime.backend_manager.current_backend._session_id = "replacement-session"
        return response

    runtime.backend_manager.generate_response = _drop_session_after_first_resume
    response = await generate_with_capacity_recovery(
        runtime,
        _item(request_content={"parts": [{"type": "image"}]}),
        "SECRET ORIGINAL PROMPT",
        {
            "is_retry": False,
            "silent": True,
            "on_stream_event": None,
            "request_content": {"parts": [{"type": "image"}]},
        },
        sleep=AsyncMock(),
    )

    assert response.is_success is False
    assert len(runtime.backend_manager.calls) == 2
    assert (
        response.stream_metadata["capacity_recovery"]["blocking_reason"]
        == policy_owner.REASON_RESUME_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_auto_uses_one_exact_fallback_route_after_retries(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_capacity(), _capacity(), _capacity(), _capacity()],
    )
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
        provider="deepseek-api",
        model="deepseek-v4-flash",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )

    async def _sleep(_seconds):
        return None

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=_sleep,
    )
    assert response.text == "fallback done"
    assert len(runtime.backend_manager.fallback_calls) == 1
    call = runtime.backend_manager.fallback_calls[0]
    assert call["target_kind"] == policy_owner.TARGET_KIND_HERV3_MODEL
    assert call["provider"] == "deepseek-api"
    assert call["model"] == "deepseek-v4-flash"
    assert runtime.config.active_backend == "codex-cli"


@pytest.mark.asyncio
async def test_auto_can_use_an_exact_alternative_codex_model(tmp_path):
    runtime = _runtime(
        tmp_path,
        [_capacity(), _capacity(), _capacity(), _capacity()],
    )
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
        model="gpt-5.1-codex",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )

    assert response.text == "fallback done"
    call = runtime.backend_manager.fallback_calls[0]
    assert call["target_kind"] == policy_owner.TARGET_KIND_CODEX_CLI
    assert call["model"] == "gpt-5.1-codex"
    assert runtime.config.active_backend == "codex-cli"


@pytest.mark.asyncio
async def test_auto_skips_a_stale_route_that_now_matches_the_current_codex_model(
    tmp_path,
):
    runtime = _runtime(
        tmp_path,
        [_capacity(), _capacity(), _capacity(), _capacity()],
    )
    current = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
        model="gpt-5-codex",
    )
    alternative = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
        model="gpt-5.1-codex",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(current, alternative),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )

    assert response.is_success is True
    assert runtime.backend_manager.fallback_calls[0]["model"] == "gpt-5.1-codex"


@pytest.mark.asyncio
async def test_herv3_to_codex_fallback_is_executed_by_pao(tmp_path):
    runtime = _runtime(tmp_path, [_capacity()])
    runtime.config.active_backend = "her-v2"
    runtime.backend_manager.config.active_backend = "her-v2"
    runtime.backend_manager.current_backend.model = "deepseek-v4-flash"
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_HERV3,
        target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
        model="gpt-5.1-codex",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )
    content = {"parts": [{"type": "text", "text": "preserve me"}]}

    response = await generate_with_capacity_recovery(
        runtime,
        _item(request_content=content),
        "original HERV3 prompt",
        {
            "is_retry": False,
            "silent": True,
            "on_stream_event": None,
            "request_content": content,
        },
        sleep=AsyncMock(),
    )

    assert response.text == "fallback done"
    assert len(runtime.backend_manager.calls) == 1
    assert len(runtime.backend_manager.fallback_calls) == 1
    call = runtime.backend_manager.fallback_calls[0]
    assert call["target_kind"] == policy_owner.TARGET_KIND_CODEX_CLI
    assert call["model"] == "gpt-5.1-codex"
    assert call["prompt"] == "original HERV3 prompt"
    assert call["request_content"] == content


@pytest.mark.asyncio
async def test_herv3_default_off_never_crosses_engine(tmp_path):
    runtime = _runtime(tmp_path, [_capacity()])
    runtime.config.active_backend = "her-v2"
    runtime.backend_manager.config.active_backend = "her-v2"

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original HERV3 prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )

    assert response.error_code == policy_owner.CAPACITY_FAILURE_CODE
    assert runtime.backend_manager.fallback_calls == []


@pytest.mark.asyncio
async def test_herv3_to_codex_fallback_preserves_run_cancellation(tmp_path):
    runtime = _runtime(tmp_path, [_capacity()])
    runtime.config.active_backend = "her-v2"
    runtime.backend_manager.config.active_backend = "her-v2"
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_HERV3,
        target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
        model="gpt-5.1-codex",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )

    async def _cancel(**_kwargs):
        raise asyncio.CancelledError

    runtime.backend_manager.generate_fallback_response = _cancel
    with pytest.raises(asyncio.CancelledError):
        await generate_with_capacity_recovery(
            runtime,
            _item(),
            "original HERV3 prompt",
            {"is_retry": False, "silent": True, "on_stream_event": None},
            sleep=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_herv3_cost_handoff_limits_the_later_pao_codex_route(tmp_path):
    initial = _capacity()
    initial.stream_metadata = {
        "her_v2": {
            "fallback_accounting": {
                "additional_calls_used": 1,
                "estimated_cost_used_usd": 0.03,
                "route_keys": [
                    "herv3|herv3_model|openrouter-api|prior-model"
                ],
            }
        }
    }
    runtime = _runtime(tmp_path, [initial])
    runtime.config.active_backend = "her-v2"
    runtime.backend_manager.config.active_backend = "her-v2"
    runtime.backend_manager.current_backend.model = "deepseek-v4-flash"
    runtime.backend_manager.config.allowed_backends[0][
        "estimated_fallback_call_cost_usd"
    ] = 0.03
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_HERV3,
        target_kind=policy_owner.TARGET_KIND_CODEX_CLI,
        model="gpt-5.1-codex",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            max_additional_calls=2,
            max_extra_cost_usd=0.05,
        ),
    )

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original HERV3 prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )

    assert runtime.backend_manager.fallback_calls == []
    assert (
        response.stream_metadata["capacity_recovery"]["blocking_reason"]
        == policy_owner.REASON_BUDGET_EXHAUSTED
    )


@pytest.mark.asyncio
async def test_fallback_cost_ceiling_is_cumulative_across_attempted_targets(
    tmp_path,
):
    runtime = _runtime(
        tmp_path,
        [_capacity(), _capacity(), _capacity(), _capacity()],
    )
    first = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
        provider="deepseek-api",
        model="deepseek-v4-flash",
    )
    second = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
        provider="openrouter-api",
        model="deepseek/deepseek-v4-flash",
    )
    runtime.backend_manager.get_her_v2_provider_options = lambda: [
        {
            "engine": "deepseek-api",
            "models": ["deepseek-v4-flash"],
            "available": True,
            "estimated_fallback_call_cost_usd": 0.03,
        },
        {
            "engine": "openrouter-api",
            "models": ["deepseek/deepseek-v4-flash"],
            "available": True,
            "estimated_fallback_call_cost_usd": 0.03,
        },
    ]
    fallback_responses = [_capacity(), _success("must not run")]

    async def _fallback(**kwargs):
        runtime.backend_manager.fallback_calls.append(dict(kwargs))
        return fallback_responses.pop(0)

    runtime.backend_manager.generate_fallback_response = _fallback
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(first, second),
            max_additional_calls=2,
            max_extra_cost_usd=0.05,
            allow_unknown_cost=False,
        ),
    )

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )

    assert response.is_success is False
    assert len(runtime.backend_manager.fallback_calls) == 1
    recovery = response.stream_metadata["capacity_recovery"]
    assert recovery["blocking_reason"] == policy_owner.REASON_BUDGET_EXHAUSTED
    assert recovery["fallback_attempts"][0]["estimated_cost_usd"] == 0.03
    assert recovery["fallback_attempts"][0]["estimated_cost_used_usd"] == 0.03


@pytest.mark.asyncio
async def test_effect_mode_b_never_switches_target_after_any_observed_effect(
    tmp_path,
):
    runtime = _runtime(
        tmp_path,
        [
            _capacity(effects=True),
            _capacity(),
            _capacity(),
            _capacity(),
        ],
    )
    route = policy_owner.FallbackRoute(
        source=policy_owner.SOURCE_CODEX_CLI,
        target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
        provider="deepseek-api",
        model="deepseek-v4-flash",
    )
    _save(
        runtime,
        policy_owner.FallbackPolicy(
            mode=policy_owner.MODE_AUTO,
            routes=(route,),
            sources={
                policy_owner.SOURCE_CODEX_CLI: policy_owner.FallbackSourceRule(
                    side_effect_mode=policy_owner.EFFECT_RESUME
                ),
                policy_owner.SOURCE_HERV3: policy_owner.FallbackSourceRule(),
            },
            max_additional_calls=1,
            allow_unknown_cost=True,
        ),
    )

    response = await generate_with_capacity_recovery(
        runtime,
        _item(),
        "original prompt",
        {"is_retry": False, "silent": True, "on_stream_event": None},
        sleep=AsyncMock(),
    )

    assert response.error_code == "PROVIDER_CAPACITY_UNAVAILABLE"
    assert runtime.backend_manager.fallback_calls == []
    assert (
        response.stream_metadata["capacity_recovery"]["blocking_reason"]
        == policy_owner.REASON_UNSAFE_CHECKPOINT
    )
