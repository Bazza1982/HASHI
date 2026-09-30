from __future__ import annotations

import pytest

from orchestrator.frontend_live_voice.delegation_policy import (
    DelegationRoute,
    is_affirmative_confirmation,
    route_delegation,
)
from orchestrator.frontend_live_voice.openai_live import fit_live_session_input


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("不用查，知道什么就说什么", DelegationRoute.DIRECT),
        ("你怎么还在兜圈子，赶紧说", DelegationRoute.DIRECT),
        ("告诉我你不说的原因", DelegationRoute.DIRECT),
        ("给我详细报告一下今天的情况", DelegationRoute.CONFIRM),
        ("给我更新一下今天的情况", DelegationRoute.CONFIRM),
        ("what is today's status?", DelegationRoute.CONFIRM),
        ("查一下今天有没有新邮件", DelegationRoute.EXECUTE),
        ("快说，帮我查一下最新日志", DelegationRoute.EXECUTE),
        ("inspect the current logs", DelegationRoute.EXECUTE),
        ("deploy the build to production", DelegationRoute.EXECUTE),
    ],
)
def test_delegation_policy_is_fail_safe_for_live_conversation(text, route):
    assert route_delegation(text).route is route


def test_unknown_live_utterance_stays_in_the_foreground():
    decision = route_delegation("把刚才那三件事详细讲清楚")
    assert decision.route is DelegationRoute.DIRECT
    assert decision.confidence >= 0.9


@pytest.mark.parametrize("text", ["好", "好的。", "yes please", "go ahead"])
def test_short_affirmation_can_confirm_a_pending_backend_check(text):
    assert is_affirmative_confirmation(text) is True


def test_zhaojun_incident_replay_cannot_create_a_delegation_storm():
    utterances = [
        "Okay 给我报告一下今天的情况",
        "不用这么查，知道什么就说什么",
        "所以从昨天聊天到现在没有任何新的信息进到你的窗口里面吗",
        "你能不要核对吗，有就是有，没有就是没有",
        "你怎么还在兜圈子，赶紧说",
        "不行，你要详细地说",
        "快说呀",
        "快说快说快说",
        "告诉我你不说的原因",
        "不要等确认，知道什么说什么",
        "把这些东西详细地报告给我",
        "不要光说有几笔，我要详细的信息",
        "快说快说",
    ]

    routes = [route_delegation(text).route for text in utterances]

    assert routes == [DelegationRoute.CONFIRM] + [DelegationRoute.DIRECT] * 12


@pytest.mark.asyncio
async def test_optional_background_reference_yields_before_mandatory_context(
    monkeypatch,
):
    weights = {"MANDATORY": 7_800, "BACKGROUND": 1_000}

    async def fake_count(
        _http,
        *,
        key,
        model,
        input_messages,
    ):
        del key, model
        return sum(
            weights[part["text"]]
            for item in input_messages
            for part in item["content"]
        )

    monkeypatch.setattr(
        "orchestrator.frontend_live_voice.openai_live._provider_input_token_count",
        fake_count,
    )

    def item(text):
        return {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        }

    fitted, audit = await fit_live_session_input(
        object(),
        key="test-key",
        model="gpt-live-1",
        input_messages=[item("MANDATORY"), item("BACKGROUND")],
        required_message_count=1,
        history_unit_message_counts=[1],
        optional_prefix_unit_count=1,
    )

    assert [entry["content"][0]["text"] for entry in fitted] == ["MANDATORY"]
    assert audit["history_included_units"] == 0
    assert audit["history_omitted_units"] == 1
