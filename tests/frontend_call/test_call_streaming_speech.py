import asyncio

import pytest

from test_call_contract import wav
from test_call_service import finish_task, setup, start


@pytest.mark.asyncio
async def test_complete_answer_sentence_starts_tts_before_run_finishes(tmp_path):
    service, ports, adapters, base, _clock = setup(tmp_path)
    binding, _, _ = await start(service, base)
    released = asyncio.Event()
    calls = 0

    def result(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if not released.is_set():
            return {
                "terminal": False,
                "state": "running",
                "text": "",
                "activity_sequence": 2,
                "events": [{
                    "sequence": 2,
                    "delivery_class": "answer_preview",
                    "presentation_channel": "answer",
                    "summary": "第一句已经完整。第二句还在生成",
                }],
            }
        return {
            "terminal": True,
            "state": "completed",
            "text": "第一句已经完整。第二句还在生成，现已完成。",
            "activity_sequence": 3,
            "events": [],
        }

    ports.result = result
    try:
        await service.invoke("owner", {
            **binding, "operation": "turn", "turn_id": "turn-1",
            "sequence": 1, "audio_b64": wav(),
        })
        for _ in range(100):
            call = service.calls["call-1"]
            if adapters.tts and call.turn.get("speech_segments"):
                break
            await asyncio.sleep(0.001)
        call = service.calls["call-1"]
        assert call.turn["phase"] == "thinking"
        assert call.turn["speech_segments"] == ["第一句已经完整。"]
        assert adapters.tts == 1
        released.set()
        await finish_task(service)
        assert call.turn["speech_segments"] == [
            "第一句已经完整。",
            "第二句还在生成，现已完成。",
        ]
        assert calls >= 2
    finally:
        released.set()
        await service.close()


@pytest.mark.asyncio
async def test_preview_deltas_keep_word_boundaries_and_repeated_final_sentences(tmp_path):
    service, ports, _adapters, base, _clock = setup(tmp_path)
    binding, _, _ = await start(service, base)
    calls = 0

    def result(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "terminal": False, "state": "running", "text": "",
                "activity_sequence": 2,
                "events": [{
                    "sequence": 2,
                    "delivery_class": "answer_preview",
                    "presentation_channel": "answer",
                    "summary": "First",
                }],
            }
        if calls == 2:
            return {
                "terminal": False, "state": "running", "text": "",
                "activity_sequence": 3,
                "events": [{
                    "sequence": 3,
                    "delivery_class": "answer_preview",
                    "presentation_channel": "answer",
                    "summary": " sentence. Again.",
                }],
            }
        return {
            "terminal": True,
            "state": "completed",
            "text": "First sentence. Again. Again.",
            "activity_sequence": 4,
            "events": [],
        }

    ports.result = result
    try:
        await service.invoke("owner", {
            **binding, "operation": "turn", "turn_id": "turn-1",
            "sequence": 1, "audio_b64": wav(),
        })
        await finish_task(service)
        call = service.calls["call-1"]
        assert call.turn["speech_segments"] == [
            "First sentence.", "Again.", "Again.",
        ]
        assert call.turn["_answer_preview"] == "First sentence. Again."
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_public_commentary_is_spoken_but_reasoning_is_never_queued(tmp_path):
    service, ports, _adapters, base, _clock = setup(tmp_path)
    binding, _, _ = await start(service, base)

    def result(*_args, **_kwargs):
        return {
            "terminal": True,
            "state": "completed",
            "text": "最终答案。",
            "activity_sequence": 4,
            "events": [
                {"sequence": 2, "delivery_class": "reasoning",
                 "presentation_channel": "thinking", "summary": "内部推理不能播报。"},
                {"sequence": 3, "delivery_class": "user_commentary",
                 "presentation_channel": "commentary", "summary": "（小声）我正在核对记录，请稍等。"},
            ],
        }

    ports.result = result
    try:
        await service.invoke("owner", {
            **binding, "operation": "turn", "turn_id": "turn-1",
            "sequence": 1, "audio_b64": wav(),
        })
        await finish_task(service)
        queued = service.calls["call-1"].turn["speech_segments"]
        assert queued == ["我正在核对记录，请稍等。", "最终答案。"]
        assert all("内部推理" not in text for text in queued)
        assert any(row.get("kind") == "commentary" for row in service.calls["call-1"].rows)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_call_projection_removes_stage_directions_from_captions_and_speech(tmp_path):
    service, ports, _adapters, base, _clock = setup(tmp_path)
    binding, _, _ = await start(service, base)
    source = {
        "terminal": True,
        "state": "completed",
        "text": "（小声）李总，电话通了。预算（含 GST）不变。",
        "activity_sequence": 3,
        "events": [
            {
                "sequence": 2,
                "delivery_class": "answer_preview",
                "presentation_channel": "answer",
                "summary": "（小声）李总，电话通了。",
            },
        ],
    }

    def result(*_args, **_kwargs):
        return source

    ports.result = result
    try:
        await service.invoke("owner", {
            **binding, "operation": "turn", "turn_id": "turn-1",
            "sequence": 1, "audio_b64": wav(),
        })
        await finish_task(service)
        call = service.calls["call-1"]
        assert source["text"].startswith("（小声）")
        assert call.turn["answer"] == "李总，电话通了。预算（含 GST）不变。"
        assert call.turn["speech_segments"] == [
            "李总，电话通了。",
            "预算（含 GST）不变。",
        ]
        assistant_rows = [
            row["text"] for row in call.rows
            if row.get("speaker") == "assistant"
        ]
        assert assistant_rows[-1] == "李总，电话通了。预算（含 GST）不变。"
        assert all("小声" not in text for text in assistant_rows)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_fillers_are_prepared_in_the_selected_voice_and_locale(tmp_path):
    service, ports, adapters, base, _clock = setup(tmp_path)
    service.filler_warm_seconds = 0

    async def opening(*_args):
        return "您好，请讲。"

    ports.opening = opening
    info = await service.invoke("owner", {**base, "operation": "context"})
    start_body = {
        **base,
        "operation": "start",
        "call_id": "call-1",
        "generation": info["generation"],
        "revision": info["revision"],
        "allow_cloud": False,
        "locale": "zh-CN",
    }
    binding = {
        **base,
        "call_id": "call-1",
        "generation": info["generation"],
    }

    try:
        await service.invoke("owner", start_body)
        call = service.calls["call-1"]
        await call.task
        if call.speech_task:
            await call.speech_task
        if call.filler_task:
            await call.filler_task

        expected = ("嗯……", "让我想一想……", "我还在处理，请稍等一下。")
        assert adapters.tts == 1 + len(expected)
        for slot, text in enumerate(expected):
            asset = await service.invoke(
                "owner", {**binding, "operation": "filler", "slot": slot}
            )
            assert asset["ready"] is True
            assert asset["text"] == text
    finally:
        await service.close()
