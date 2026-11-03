"""One call's synthesis/publication/delivery result; no shared last-error state."""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class VoiceReplyResult:
    delivered: bool | None
    stage: str = "delivery"
    code: str = ""
    reason: str = ""
    reference: str = ""


def voice_reply_failure(stage: str, code: str, reason: str, *, uncertain: bool = False) -> VoiceReplyResult:
    safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", code) else "voice_reply_failed"
    return VoiceReplyResult(
        None if uncertain else False, stage, safe_code, str(reason)[:1200],
        "voicerr_" + uuid4().hex[:24],
    )


def render_voice_reply_result(result: VoiceReplyResult) -> str:
    from orchestrator import ui_language

    stage = result.stage if result.stage in {
        "configuration", "synthesis", "prepare", "publication", "delivery",
    } else "delivery"
    message = ui_language.tr(
        "voice.reply_unconfirmed_detailed" if result.delivered is None else "voice.reply_failed_detailed",
        stage=ui_language.tr("voice.reply.stage." + stage),
        code=result.code or ("voice_delivery_unconfirmed" if result.delivered is None else "voice_reply_failed"),
        reason=result.reason or ui_language.tr("voice.reply.reason.unavailable"),
    )
    if result.reference:
        message += "\n" + ui_language.tr("voice.reply.reference", reference=result.reference)
    return message
