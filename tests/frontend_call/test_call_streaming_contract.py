from orchestrator.frontend_call.contract import (
    complete_speech_segments,
    filler_phrases,
    speech_segments,
)


def test_streaming_only_releases_complete_sentences_in_order():
    first = "第一句已经完整，而且长度足够自然，可以立即开始播报。"
    parts, remainder = complete_speech_segments(
        first + "第二句还没有"
    )
    assert parts == [first]
    assert remainder == "第二句还没有"


def test_streaming_holds_short_sentences_until_the_chunk_is_natural():
    short = "正在检查。请稍等。"
    parts, remainder = complete_speech_segments(short)
    assert parts == []
    assert remainder == short

    natural = short + "查到完整结果后马上告诉您，请放心。"
    parts, remainder = complete_speech_segments(natural)
    assert parts == [natural]
    assert remainder == ""


def test_final_answer_coalesces_adjacent_short_sentences():
    text = "测试官，查完了。当前服务在线。"
    parts, truncated = speech_segments(text)
    assert parts == [text]
    assert not truncated


def test_final_answer_keeps_short_english_sentences_in_one_playable_chunk():
    text = "Fashion is on mark. Camera is stable. All test is complete."
    parts, truncated = speech_segments(text)
    assert parts == [text]
    assert not truncated

    first = (
        "This complete sentence has enough spoken words to begin playback "
        "while the answer continues."
    )
    stable, remainder = complete_speech_segments(first + " Tail still changing")
    assert stable == [first]
    assert remainder == "Tail still changing"


def test_streaming_chunks_remain_a_prefix_of_the_final_chunks():
    first = "这是一句长度足够自然的完整开场，可以在回答仍生成时先播报。"
    short = "好的。"
    third = "接下来这一句会与前面的短句合并，避免再次启动语音造成空白。"
    final, truncated = speech_segments(first + short + third)
    assert not truncated
    for preview in (first, first + short, first + short + third):
        stable, _remainder = complete_speech_segments(preview)
        assert final[:len(stable)] == stable


def test_final_answer_groups_short_sentences_into_a_natural_tts_item():
    parts, truncated = speech_segments(
        "第一句很短。第二句也完整！ Third sentence is complete."
    )
    assert parts == [
        "第一句很短。第二句也完整！ Third sentence is complete.",
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
        "李总，电话通了。后续预算（含 GST）保持不变。",
    ]
    assert not truncated


def test_speech_preserves_meaningful_leading_parentheses():
    parts, truncated = speech_segments("(Q3 revenue) increased by 12%.")
    assert parts == ["(Q3 revenue) increased by 12%."]
