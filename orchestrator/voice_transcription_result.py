"""Immutable per-call transcription facts; no PAO authority or external dependencies."""
from __future__ import annotations

import re
import math
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

_MESSAGES = {
    "TRANSCRIPTION_INPUT_UNAVAILABLE": "The audio input is unavailable or invalid.",
    "TRANSCRIPTION_ACCESS_DENIED": "Access to a file or component required for transcription was denied.",
    "TRANSCRIPTION_DEPENDENCY_MISSING": "A required transcription component is unavailable.",
    "TRANSCRIPTION_CONFIGURATION_INVALID": "The transcription configuration is invalid or unreadable.",
    "TRANSCRIPTION_MODEL_LOAD_FAILED": "The configured transcription model could not be loaded.",
    "TRANSCRIPTION_TIMEOUT": "The transcription service did not respond before its deadline.",
    "TRANSCRIPTION_RUNTIME_UNAVAILABLE": "The configured isolated transcription runtime is unavailable.",
    "TRANSCRIPTION_SERVICE_UNAVAILABLE": "The selected transcription service is unavailable.",
    "TRANSCRIPTION_PROTOCOL_ERROR": "The transcription service returned an invalid result.",
    "TRANSCRIPTION_EMPTY": "The recording produced no transcript text.",
    "TRANSCRIPTION_FAILED": "Audio transcription failed; inspect the diagnostic reference.",
    "TRANSCRIPT_RECORDING_FAILED": "The transcript could not be recorded in its original Session.",
}
_PHASES = frozenset({"input", "configuration", "model", "local", "external", "compute", "worker", "processing", "record"})
_DEPENDENCIES = frozenset({"faster_whisper", "ctranslate2", "av", "onnxruntime", "numpy", "tokenizers", "huggingface_hub"})
_REFERENCE = re.compile(r"^stterr_[0-9a-f]{24}$")
_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")


@dataclass(frozen=True)
class TranscriptionFailure:
    code: str
    phase: str
    reference: str
    error_type: str
    dependency: str = ""

    @property
    def message(self) -> str:
        message = _MESSAGES[self.code]
        return message + (f" Component: {self.dependency}." if self.dependency else "")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "phase": self.phase, "reference": self.reference,
                "error_type": self.error_type, **({"dependency": self.dependency} if self.dependency else {})}

    @classmethod
    def from_dict(cls, value: Any) -> TranscriptionFailure | None:
        if not isinstance(value, Mapping):
            return None
        code, phase = value.get("code"), value.get("phase")
        reference, error_type = value.get("reference"), value.get("error_type")
        dependency = value.get("dependency", "")
        if (not isinstance(code, str) or not isinstance(phase, str) or not isinstance(dependency, str) or
                code not in _MESSAGES or phase not in _PHASES or
                not isinstance(reference, str) or not _REFERENCE.fullmatch(reference) or
                not isinstance(error_type, str) or not _TYPE.fullmatch(error_type) or
                dependency not in _DEPENDENCIES | {""}):
            return None
        return cls(code, phase, reference, error_type, dependency)



def first_transcription_failure(parts: Any) -> TranscriptionFailure | None:
    for part in parts:
        failure = TranscriptionFailure.from_dict(part.get("failure")) if isinstance(part, Mapping) else None
        if failure is not None:
            return failure
    return None


class TranscriptionFailed(RuntimeError):
    def __init__(self, failure: TranscriptionFailure):
        self.failure = failure
        super().__init__(f"[{failure.code}] {failure.message} Diagnostic reference: {failure.reference}")


class TranscriptionProtocolError(RuntimeError):
    pass


def transcription_failure(error: Exception, *, phase: str) -> TranscriptionFailure:
    if isinstance(error, TranscriptionFailed):
        return error.failure
    if phase not in _PHASES:
        phase = "processing"
    dependency = ""
    if phase == "record":
        code = "TRANSCRIPT_RECORDING_FAILED"
    elif isinstance(error, ImportError):
        code = "TRANSCRIPTION_DEPENDENCY_MISSING"
        name = str(getattr(error, "name", "") or "").split(".")[0]
        dependency = name if name in _DEPENDENCIES else ""
    elif isinstance(error, (TimeoutError,)):
        code = "TRANSCRIPTION_TIMEOUT"
    elif isinstance(error, TranscriptionProtocolError):
        code = "TRANSCRIPTION_PROTOCOL_ERROR"
    elif isinstance(error, PermissionError):
        code = "TRANSCRIPTION_ACCESS_DENIED"
    elif phase == "configuration":
        code = "TRANSCRIPTION_CONFIGURATION_INVALID"
    elif phase == "model":
        code = "TRANSCRIPTION_MODEL_LOAD_FAILED"
    elif phase == "compute":
        code = "TRANSCRIPTION_SERVICE_UNAVAILABLE"
    elif phase == "external":
        code = "TRANSCRIPTION_RUNTIME_UNAVAILABLE"
    elif isinstance(error, (FileNotFoundError, ValueError)):
        code = "TRANSCRIPTION_INPUT_UNAVAILABLE"
    else:
        code = "TRANSCRIPTION_FAILED"
    error_type = type(error).__name__
    return TranscriptionFailure(code, phase, "stterr_" + uuid4().hex[:24],
                                error_type if _TYPE.fullmatch(error_type) else "Exception", dependency)


@dataclass(frozen=True)
class TranscriptionSegment:
    start: float
    end: float
    text: str


def transcription_segments(value):
    if not isinstance(value, (list, tuple)) or len(value) > 20000:
        raise TranscriptionProtocolError()
    result = []
    previous = 0.0
    size = 0
    for row in value:
        if not isinstance(row, dict):
            raise TranscriptionProtocolError()
        start, end, text = row.get("start"), row.get("end"), row.get("text")
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous or end < start or end > 8640000
                or not isinstance(text, str)):
            raise TranscriptionProtocolError()
        size += len(text.encode("utf-8"))
        if size > 524288:
            raise TranscriptionProtocolError()
        previous = float(start)
        if text.strip():
            result.append(TranscriptionSegment(float(start), float(end), text.strip()))
    return tuple(result)


@dataclass(frozen=True)
class TranscriptionResult:
    text: str = ""
    failure: TranscriptionFailure | None = None
    segments: tuple[TranscriptionSegment, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.segments, tuple) or not all(isinstance(row, TranscriptionSegment) for row in self.segments):
            raise TypeError("invalid transcription segments")
        if not isinstance(self.text, str) or (self.failure is not None and not isinstance(self.failure, TranscriptionFailure)):
            raise TypeError("invalid transcription result")
        if self.failure is None and not self.text.strip():
            object.__setattr__(self, "failure", TranscriptionFailure(
                "TRANSCRIPTION_EMPTY", "processing", "stterr_" + uuid4().hex[:24], "EmptyTranscript"))
        if self.failure is None:
            object.__setattr__(self, "text", self.text.strip())

    @property
    def ok(self) -> bool:
        return self.failure is None and isinstance(self.text, str) and bool(self.text.strip())

    @classmethod
    def from_text(cls, text: Any) -> TranscriptionResult:
        if isinstance(text, str) and text.strip() and not text.startswith("[Transcription error]"):
            return cls(text.strip())
        error = RuntimeError() if isinstance(text, str) else TranscriptionProtocolError()
        failure = transcription_failure(error, phase="processing")
        if isinstance(text, str) and not text.strip():
            failure = TranscriptionFailure("TRANSCRIPTION_EMPTY", "processing", failure.reference, "EmptyTranscript")
        return cls(failure=failure)

    def legacy_text(self) -> str:
        return self.text if self.ok else "[Transcription error] " + str(TranscriptionFailed(self.failure))

    def attachment_result(self, attachment_id: str) -> dict[str, Any]:
        return {"attachment_id": attachment_id, "text": self.text if self.ok else "",
                "error": "" if self.ok else str(TranscriptionFailed(self.failure)),
                **({"failure": self.failure.as_dict()} if self.failure else {})}


async def transcribe_audio(transcriber: Any, audio_path: Any) -> TranscriptionResult:
    """Use the typed API, retaining compatibility with existing embedded transcribers."""
    try:
        method = getattr(transcriber, "transcribe_result", None)
        if callable(method):
            result = await method(audio_path)
            if isinstance(result, TranscriptionResult):
                return result if result.ok or result.failure is not None else TranscriptionResult.from_text("")
            raise TranscriptionProtocolError()
        return TranscriptionResult.from_text(await transcriber.transcribe(audio_path))
    except Exception as error:
        return TranscriptionResult(failure=transcription_failure(error, phase="processing"))
