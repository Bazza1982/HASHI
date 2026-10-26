import pytest

from orchestrator.frontend_call.contract import (
    complete_video_speech_segments,
    complete_speech_segments,
    FILLER_TIER_SIZE,
    filler_phrases,
    speech_segments,
    video_speech_segments,
)


def test_streaming_only_releases_complete_sentences_in_order():
    first = "第一句已经完整，而且长度足够自然，可以立即开始播报。"
    parts, remainder = complete_speech_segments(first + "第二句还没有")
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


def test_video_speech_honours_renderer_duration_boundary_without_changing_audio():
    text = "测试官，晚上好，露米在这里。今天想聊点什么，或者有什么要我来安排的？"
    audio_parts, audio_truncated = speech_segments(text)
    video_parts, video_truncated = speech_segments(text, maximum_duration_units=20)

    assert audio_parts == [text]
    assert video_parts == [
        "测试官，晚上好，露米在这里。",
        "今天想聊点什么，或者有什么要我来安排的？",
    ]
    assert not audio_truncated and not video_truncated


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
        assert final[: len(stable)] == stable


def test_video_streaming_releases_configured_complete_sentence_groups():
    sentences = [f"第{index}句。" for index in range(1, 11)]
    stable, remainder = complete_video_speech_segments(
        "".join(sentences),
        first_segment_sentences=1,
        group_sentences=4,
    )

    assert stable == [
        sentences[0],
        "".join(sentences[1:5]),
        "".join(sentences[5:9]),
    ]
    assert remainder == sentences[9]


@pytest.mark.parametrize(
    "sentence_count,expected_sizes",
    [
        (6, [1, 4, 1]),
        (10, [1, 4, 4, 1]),
        (12, [1, 4, 4, 3]),
        (20, [1, 4, 4, 4, 4, 3]),
    ],
)
def test_video_final_answer_uses_tunable_sentence_groups(
    sentence_count, expected_sizes
):
    sentences = [f"第{index}句。" for index in range(1, sentence_count + 1)]
    parts, truncated = video_speech_segments(
        "".join(sentences),
        first_segment_sentences=1,
        group_sentences=4,
    )

    assert not truncated
    assert [part.count("句。") for part in parts] == expected_sizes
    assert "".join(parts) == "".join(sentences)


def test_final_answer_groups_short_sentences_into_a_natural_tts_item():
    parts, truncated = speech_segments(
        "第一句很短。第二句也完整！ Third sentence is complete."
    )
    assert parts == [
        "第一句很短。第二句也完整！ Third sentence is complete.",
    ]
    assert not truncated


def test_fillers_are_localized_and_do_not_claim_an_answer():
    simplified = filler_phrases("zh-CN")
    assert FILLER_TIER_SIZE == 10
    assert len(simplified) == 3 * FILLER_TIER_SIZE
    assert simplified[:10] == (
        "嗯……",
        "唔……",
        "哦……",
        "呃……",
        "这个……",
        "让我想想……",
        "这个嘛……",
        "嗯，我想想……",
        "我想想啊……",
        "怎么说呢……",
    )
    assert simplified[10:20] == (
        "嗯……我想想啊。",
        "哦，这个啊……",
        "嗯……怎么说呢。",
        "这个……让我想一下。",
        "哦……让我再想一想。",
        "嗯……我先把思路理顺。",
        "唔……我琢磨一下。",
        "这个嘛……我再琢磨一下。",
        "嗯……我想想怎么说比较好。",
        "哦……让我理一下思路。",
    )
    assert simplified[20:] == (
        "嗯……这个我得稍微想一下，怎么说比较合适呢……",
        "哦……让我稍微整理一下思路，再接着说。",
        "嗯……我先把思路理一理，再看看怎么回答。",
        "这个嘛……我想先把思路理顺一点，再跟你说。",
        "唔……让我再琢磨一下，想想怎么表达更合适。",
        "嗯……让我稍微整理一下，再接着回答。",
        "哦……我在想怎么把这件事讲得更清楚一点。",
        "嗯……我先想想怎么解释得更自然一点。",
        "这个……让我想一下从哪里开始说会比较好。",
        "嗯……我先把思路串起来，再慢慢跟你讲。",
    )
    for locale in ("en", "zh-CN", "zh-TW", "ja"):
        phrases = filler_phrases(locale)
        assert len(phrases) == 3 * FILLER_TIER_SIZE
        assert len(set(phrases)) == len(phrases)
    assert filler_phrases("en")[10].startswith("Hmm")
    assert filler_phrases("en")[20].startswith("Hmm")
    assert filler_phrases("zh-TW")[10].startswith("嗯")
    assert filler_phrases("ja")[10].startswith("うーん")
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
