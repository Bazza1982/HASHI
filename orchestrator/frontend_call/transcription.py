"""Interpret cloud STT evidence before it can become an Agent Message."""

import math
import re
import unicodedata


_NON_SPEECH_LABELS = frozenset(
    {"loud sound", "loud noise", "noise", "background noise", "music", "applause",
     "cough", "coughing", "breathing", "laughter", "laughing", "clapping",
     "knocking", "barking", "rustling", "beeping", "ringing", "silence",
     "噪声", "噪音", "背景噪声", "音乐", "掌声", "咳嗽声", "敲击声"}
)
_WRAPPED = re.compile(r"(?P<stars>\*{1,2})(?P<star_label>[^*\n]{1,200})(?P=stars)|\[(?P<bracket_label>[^\]\n]{1,200})\]")
_SOUND_DESCRIPTION = re.compile(
    r"^(?:(?:a|the)\s+)?(?:(?:loud|soft|quiet|background|ambient)\s+)?"
    r"(?:sounds?|noises?)(?:\s+of\s+.{1,160})?$", re.IGNORECASE
)


def spoken_transcription(result: dict) -> tuple[str, str | None]:
    """Blank results and confident cloud no-speech evidence are normal outcomes.

    Whisper's no-speech probability alone cannot veto a confident short word.
    Missing, malformed or mixed segment evidence leaves speech eligible.
    Only marked sound descriptions are removed; plain speech and unknown
    emphasis survive. This interprets the selected model's annotation convention,
    not an independent acoustic classifier or an instruction to another model.
    """
    text = result["text"].strip()
    if result.get("has_speech") is False or not text:
        return "", "no_speech"
    segments = result.get("segments")
    if isinstance(segments, list) and segments and all(
        _confident_no_speech(segment) for segment in segments
    ):
        return "", "no_speech"

    removed = False

    def replace(match):
        nonlocal removed
        label = " ".join((match["star_label"] or match["bracket_label"]).split()).casefold()
        label = label.rstrip(" .,!?:;。！？、，：；")
        if label in _NON_SPEECH_LABELS or _SOUND_DESCRIPTION.fullmatch(label):
            removed = True
            return " "
        return match[0]

    spoken = _WRAPPED.sub(replace, text).strip()
    if removed:
        spoken = re.sub(r"[ \t]{2,}", " ", spoken)
        if all(char.isspace() or unicodedata.category(char).startswith("P") for char in spoken):
            spoken = ""
    return spoken, "non_speech_annotation" if removed and not spoken else None


def _confident_no_speech(segment):
    if not isinstance(segment, dict):
        return False
    probability, logprob = segment.get("no_speech_prob"), segment.get("avg_logprob")
    return (
        type(probability) in (int, float) and math.isfinite(probability)
        and .6 < probability <= 1
        and type(logprob) in (int, float) and math.isfinite(logprob)
        and logprob < -1
    )
