from orchestrator.frontend_call.contract import (
    complete_speech_segments,
    filler_phrases,
    speech_segments,
)


def test_streaming_only_releases_complete_sentences_in_order():
    parts, remainder = complete_speech_segments(
        "第一句已经完整。第二句还没有"
    )
    assert parts == ["第一句已经完整。"]
    assert remainder == "第二句还没有"


def test_final_answer_queues_one_complete_sentence_per_tts_item():
    parts, truncated = speech_segments(
        "第一句很短。第二句也完整！ Third sentence is complete."
    )
    assert parts == [
        "第一句很短。",
        "第二句也完整！",
        "Third sentence is complete.",
    ]
    assert not truncated


def test_fillers_are_localized_and_do_not_claim_an_answer():
    assert filler_phrases("zh-CN") == ("嗯……", "让我想一想……", "我还在处理，请稍等一下。")
    assert filler_phrases("unknown") == filler_phrases("en")


def test_speech_removes_only_leading_stage_directions():
    parts, truncated = speech_segments(
        "（小声） (whispers) *sighs* 李总，电话通了。后续预算（含 GST）保持不变。"
    )
    assert parts == [
        "李总，电话通了。",
        "后续预算（含 GST）保持不变。",
    ]
    assert not truncated


def test_speech_preserves_meaningful_leading_parentheses():
    parts, truncated = speech_segments("(Q3 revenue) increased by 12%.")
    assert parts == ["(Q3 revenue) increased by 12%."]
