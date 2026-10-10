"""First audio should not wait for synthesis of the whole final answer."""
from orchestrator.frontend_call.contract import speech_segments


def test_first_audio_is_a_short_natural_phrase_without_losing_the_answer():
    text = "我们可以继续聊这个问题，先从您刚才提到的变化说起。" + "接下来再一起确认时间和具体表现。" * 18
    parts, truncated = speech_segments(text)
    assert parts[0] == "我们可以继续聊这个问题，先从您刚才提到的变化说起。"
    assert "".join(parts) == text
    assert not truncated
    assert all(len(part) <= 600 for part in parts)


def test_english_first_phrase_keeps_words_and_the_rest_of_the_answer():
    first = "Let us start with the changes you noticed."
    text = first + " Then we can discuss the details and decide what to check next." * 9
    parts, truncated = speech_segments(text)
    assert parts[0] == first
    assert " ".join(parts) == text
    assert not truncated


def test_long_unpunctuated_opening_is_bounded_and_does_not_split_english_words():
    text = "Please describe " + "the changes you noticed " * 30
    parts, truncated = speech_segments(text)
    assert len(parts[0]) <= 120
    assert " ".join(parts) == text.strip()
    assert not truncated


def test_short_first_sentence_does_not_leave_one_oversized_followup_segment():
    first = "这是第一句自然开场，用来尽快开始播报并保持对话自然。"
    text = first + "接下来继续说明背景、经过和需要确认的细节，让整段回答保持完整。" * 8
    parts, truncated = speech_segments(text)
    assert parts[0] == first
    assert len(parts) > 2
    assert len(parts[1]) <= len(parts[0]) * 2
    assert "".join(parts) == text
    assert not truncated
