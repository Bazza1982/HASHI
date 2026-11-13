"""Safe presentation facts for FC failures, without retry or admission authority."""
from __future__ import annotations

import errno
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from orchestrator import ui_language
from orchestrator.frontend_contracts import FrontendCaptionError
from orchestrator.session_attachment_limits import AttachmentSizeExceeded
from orchestrator.multimodal_contract import MultimodalContractError
from orchestrator.voice_transcription_result import TranscriptionFailed

_TRANSCRIPTION_REASONS = {
    "TRANSCRIPTION_INPUT_UNAVAILABLE": "missing_file",
    "TRANSCRIPTION_ACCESS_DENIED": "transcription_access_denied",
    "TRANSCRIPTION_DEPENDENCY_MISSING": "dependency_missing",
    "TRANSCRIPTION_CONFIGURATION_INVALID": "transcription_configuration_invalid",
    "TRANSCRIPTION_MODEL_LOAD_FAILED": "transcription_model_load_failed",
    "TRANSCRIPTION_TIMEOUT": "timed_out",
    "TRANSCRIPTION_RUNTIME_UNAVAILABLE": "transcription_runtime_unavailable",
    "TRANSCRIPTION_SERVICE_UNAVAILABLE": "transcription_service_unavailable",
    "TRANSCRIPTION_PROTOCOL_ERROR": "transcription_protocol_error",
    "TRANSCRIPTION_EMPTY": "transcription_empty",
    "TRANSCRIPT_RECORDING_FAILED": "transcript_recording_failed",
    "TRANSCRIPTION_FAILED": "transcription_failed"
}

_PHASES = frozenset({"download", "validate", "store", "admit", "transcribe", "publish", "deliver", "read", "save", "cleanup"})
_TYPES = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_MEDIA_REASONS = {
    "MEDIA_UNAVAILABLE": "missing_file",
    "MEDIA_PATH_NOT_AUTHORIZED": "access_denied",
    "MEDIA_INTEGRITY_CHANGED": "integrity_changed",
    "MEDIA_SIGNATURE_MISMATCH": "format_mismatch",
    "MEDIA_SIGNATURE_UNVERIFIED": "format_unverified",
    "MEDIA_LIMIT_EXCEEDED": "media_too_large",
    "MEDIA_TRANSPORT_UNSUPPORTED": "transport_unsupported",
    "MEDIA_POLICY_BLOCKED": "policy_blocked",
    "DUPLICATE_MEDIA_REFERENCE": "invalid_input",
    "INLINE_MEDIA_PERSISTENCE_FORBIDDEN": "invalid_input",
    "INVALID_MULTIMODAL_CONTENT": "invalid_input",
}


class AdmissionReceiptUnavailable(RuntimeError):
    """The caller lacks proof of admission; this never authorizes replay."""


def _reason(error: Exception) -> str:
    from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, TimedOut
    from orchestrator.session_store import SessionConflict, SessionNotFound

    if isinstance(error, TranscriptionFailed):
        return _TRANSCRIPTION_REASONS[error.failure.code]
    if isinstance(error, AdmissionReceiptUnavailable):
        return "receipt_unavailable"
    if isinstance(error, FrontendCaptionError):
        return {"caption_too_long": "caption_too_long", "caption_empty": "caption_empty",
                "caption_control_character": "caption_control"}.get(error.error_code, "invalid_input")
    if isinstance(error, MultimodalContractError):
        if error.code == "MEDIA_UNAVAILABLE" and isinstance(error.__cause__, OSError):
            return _reason(error.__cause__)
        return _MEDIA_REASONS.get(error.code, "invalid_input")
    if isinstance(error, AttachmentSizeExceeded):
        return {
            "telegram": "telegram_file_too_large",
            "total": "attachments_too_large",
        }.get(error.scope, "attachment_too_large")
    if isinstance(error, SessionConflict):
        if isinstance(error.__cause__, (OSError, AttachmentSizeExceeded)):
            return _reason(error.__cause__)
        return "state_changed"
    if isinstance(error, SessionNotFound):
        return "scope_unavailable"
    if isinstance(error, (TimedOut, TimeoutError)):
        return "timed_out"
    if isinstance(error, (Forbidden, InvalidToken)):
        return "service_rejected"
    if isinstance(error, BadRequest):
        return "request_rejected"
    if isinstance(error, (NetworkError, ConnectionError)):
        return "network_unavailable"
    if isinstance(error, FileNotFoundError):
        return "missing_file"
    if isinstance(error, PermissionError):
        return "access_denied"
    if isinstance(error, OSError) and (error.errno == errno.ENOSPC or getattr(error, "winerror", None) == 112):
        return "disk_full"
    if isinstance(error, ImportError):
        return "dependency_missing"
    if isinstance(error, ValueError):
        return "invalid_input"
    return "internal_error"


@dataclass(frozen=True)
class FrontendFailure:
    phase: str
    reason: str
    submission_state: Literal["not_submitted", "unconfirmed", "submitted"]
    reference: str
    error_type: str
    limit: int | None = None
    error_code: str = ""
    actual_bytes: int | None = None
    maximum_bytes: int | None = None

    def diagnostic(self) -> dict[str, Any]:
        return {"reference": self.reference, "phase": self.phase, "reason": self.reason,
                "submission_state": self.submission_state, "error_type": self.error_type,
                **({"error_code": self.error_code} if self.error_code else {}),
                **({"actual_bytes": self.actual_bytes, "maximum_bytes": self.maximum_bytes}
                   if self.maximum_bytes is not None else {})}

    def render(self, *, locale: str | None = None) -> str:
        return ui_language.tr(
            "frontend.failure.detail", locale=locale,
            stage=ui_language.tr("frontend.failure.stage." + self.phase, locale=locale),
            reason=ui_language.tr(
                "frontend.failure.reason." + self.reason, locale=locale, limit=self.limit,
                actual_bytes=self.actual_bytes, maximum_bytes=self.maximum_bytes,
            ),
            submission=ui_language.tr("frontend.failure." + self.submission_state, locale=locale),
            reference=self.reference,
        )


def frontend_failure(error: Exception, *, phase: str,
                     submission_state: Literal["not_submitted", "unconfirmed", "submitted"]) -> FrontendFailure:
    """Callers supply observed stage and acknowledgement state, never guessed effects."""
    if phase not in _PHASES or submission_state not in {"not_submitted", "unconfirmed", "submitted"}:
        raise ValueError("invalid frontend failure facts")
    error_type = type(error).__name__
    if not _TYPES.fullmatch(error_type):
        error_type = "Exception"
    maximum = getattr(error, "maximum", None) if isinstance(error, FrontendCaptionError) else None
    limit = maximum if type(maximum) is int and 1 <= maximum <= 16384 else None
    reference, error_code = "fcerr_" + uuid4().hex[:24], ""
    if isinstance(error, TranscriptionFailed):
        reference, error_code = error.failure.reference, error.failure.code
        error_type = error.failure.error_type
    from orchestrator.session_store import SessionConflict

    size_error = error
    if isinstance(error, SessionConflict) and isinstance(error.__cause__, AttachmentSizeExceeded):
        size_error = error.__cause__
    reason = _reason(error)
    if isinstance(size_error, AttachmentSizeExceeded) and size_error.actual_bytes is None:
        reason += "_unknown"
    return FrontendFailure(
        phase, reason, submission_state, reference, error_type, limit, error_code,
        size_error.actual_bytes if isinstance(size_error, AttachmentSizeExceeded) else None,
        size_error.maximum_bytes if isinstance(size_error, AttachmentSizeExceeded) else None,
    )
