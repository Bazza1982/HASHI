"""Live Call voice choices and previews from a trusted Hashi Compute node."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from orchestrator.compute_services import (
    ComputeNode,
    ComputeServiceClient,
    ComputeServiceDirectory,
    ComputeServiceError,
    ComputeServicesConfig,
)


CATALOGUE_FORMAT = "hashi-compute.speech-catalogue"
CATALOGUE_VERSION = 1
MAX_CATALOGUE_VOICES = 512
MAX_AUDITIONS_PER_VOICE = 16
_VOICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{2,8})?$")
_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
}


def _invalid() -> ComputeServiceError:
    return ComputeServiceError("speech_catalogue_invalid")


def _text(value: Any, maximum: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _invalid()
    result = value.strip()
    if (
        (required and not result)
        or len(result) > maximum
        or any(ord(char) < 32 for char in result)
    ):
        raise _invalid()
    return result


def _language(value: Any) -> str:
    result = _text(value, 24).casefold()
    if _LANGUAGE.fullmatch(result) is None:
        raise _invalid()
    return result


def _sample_path(value: Any) -> tuple[str, str]:
    path = _text(value, 500)
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "\\" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise _invalid()
    suffix = Path(parsed.path).suffix.casefold()
    media_type = _MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise _invalid()
    return parsed.path, media_type


@dataclass(frozen=True)
class VoiceAudition:
    language: str
    text: str
    path: str
    media_type: str


@dataclass(frozen=True)
class ComputeVoice:
    id: str
    label: str
    gender: str | None
    language: str
    language_name: str
    description: str
    callable: bool
    availability: str
    auditions: tuple[VoiceAudition, ...]

    def audition(self, locale: str) -> VoiceAudition | None:
        requested = str(locale or "").strip().casefold().replace("_", "-")
        requested = requested.split("-", 1)[0]
        ordered = [requested, self.language, "zh", "en", "ja"]
        for language in dict.fromkeys(item for item in ordered if item):
            for audition in self.auditions:
                if audition.language == language:
                    return audition
        return self.auditions[0] if self.auditions else None


@dataclass(frozen=True)
class SpeechCatalogue:
    model_id: str
    base_url: str
    configuration_revision: str
    voices: tuple[ComputeVoice, ...]

    @property
    def total(self) -> int:
        return len(self.voices)

    @property
    def callable_voices(self) -> tuple[ComputeVoice, ...]:
        return tuple(voice for voice in self.voices if voice.callable)

    def voice(self, voice_id: str) -> ComputeVoice:
        for voice in self.voices:
            if voice.id == voice_id:
                return voice
        raise ComputeServiceError("speech_voice_not_found")


@dataclass(frozen=True)
class DownloadedVoicePreview:
    voice: ComputeVoice
    language: str
    payload: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class ComputeVoiceDirectory:
    target_id: str
    node: ComputeNode
    catalogue: SpeechCatalogue
    client: ComputeServiceClient | Any

    async def download_preview(
        self,
        voice_id: str,
        locale: str,
    ) -> DownloadedVoicePreview:
        voice = self.catalogue.voice(voice_id)
        audition = voice.audition(locale)
        if audition is None:
            raise ComputeServiceError("speech_preview_unavailable")
        payload, media_type = await self.client.fetch_resource_bytes(
            self.node,
            audition.path,
        )
        if media_type != audition.media_type:
            raise ComputeServiceError("speech_preview_invalid")
        if media_type == "audio/wav" and not (
            payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"
        ):
            raise ComputeServiceError("speech_preview_invalid")
        if media_type == "audio/ogg" and not payload.startswith(b"OggS"):
            raise ComputeServiceError("speech_preview_invalid")
        if media_type == "audio/mpeg" and not (
            payload.startswith(b"ID3")
            or (len(payload) > 2 and payload[0] == 255 and payload[1] & 0xE0 == 0xE0)
        ):
            raise ComputeServiceError("speech_preview_invalid")
        return DownloadedVoicePreview(
            voice=voice,
            language=audition.language,
            payload=payload,
            media_type=media_type,
            filename=Path(audition.path).name,
        )


def parse_speech_catalogue(
    payload: Any,
    *,
    model_id: str,
    expected_base_url: str,
) -> SpeechCatalogue:
    if not isinstance(payload, Mapping):
        raise _invalid()
    if (
        payload.get("format") != CATALOGUE_FORMAT
        or payload.get("version") != CATALOGUE_VERSION
    ):
        raise _invalid()
    base_url = _text(payload.get("base_url"), 500).rstrip("/")
    if base_url != str(expected_base_url or "").strip().rstrip("/"):
        raise _invalid()
    revision_raw = payload.get("configuration_revision")
    if isinstance(revision_raw, bool) or not isinstance(revision_raw, (str, int)):
        raise _invalid()
    revision = str(revision_raw).strip()
    if not revision or len(revision) > 100:
        raise _invalid()
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise _invalid()
    matches = [
        item
        for item in models
        if isinstance(item, Mapping) and item.get("id") == model_id
    ]
    if len(matches) != 1:
        raise _invalid()
    rows = matches[0].get("voices")
    if not isinstance(rows, list) or len(rows) > MAX_CATALOGUE_VOICES:
        raise _invalid()
    voices: list[ComputeVoice] = []
    identifiers: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise _invalid()
        voice_id = _text(row.get("id"), 32)
        if _VOICE_ID.fullmatch(voice_id) is None or voice_id in identifiers:
            raise _invalid()
        identifiers.add(voice_id)
        label = _text(row.get("label") or voice_id, 100)
        gender_raw = row.get("gender")
        if gender_raw is None:
            gender = None
        elif isinstance(gender_raw, str) and gender_raw.strip().casefold() in {
            "female",
            "male",
            "mixed",
            "neutral",
            "nonbinary",
            "unknown",
        }:
            normalized = gender_raw.strip().casefold()
            gender = None if normalized == "unknown" else normalized
        else:
            raise _invalid()
        language = _language(row.get("language"))
        language_name = _text(
            row.get("language_name"),
            60,
            required=False,
        )
        description = _text(row.get("note"), 240, required=False)
        callable_value = row.get("callable")
        if type(callable_value) is not bool:
            raise _invalid()
        availability = _text(
            row.get("availability"),
            100,
            required=False,
        )
        audition_rows = row.get("auditions", [])
        if (
            not isinstance(audition_rows, list)
            or len(audition_rows) > MAX_AUDITIONS_PER_VOICE
        ):
            raise _invalid()
        auditions: list[VoiceAudition] = []
        seen_languages: set[str] = set()
        for audition_row in audition_rows:
            if not isinstance(audition_row, Mapping):
                raise _invalid()
            if str(audition_row.get("status") or "").strip().casefold() != "available":
                continue
            sample = audition_row.get("sample")
            if not isinstance(sample, Mapping):
                raise _invalid()
            audition_language = _language(audition_row.get("language"))
            if audition_language in seen_languages:
                raise _invalid()
            seen_languages.add(audition_language)
            path, media_type = _sample_path(sample.get("url"))
            auditions.append(
                VoiceAudition(
                    language=audition_language,
                    text=_text(
                        audition_row.get("text"),
                        1200,
                        required=False,
                    ),
                    path=path,
                    media_type=media_type,
                )
            )
        voices.append(
            ComputeVoice(
                id=voice_id,
                label=label,
                gender=gender,
                language=language,
                language_name=language_name,
                description=description,
                callable=callable_value,
                availability=availability,
                auditions=tuple(auditions),
            )
        )
    return SpeechCatalogue(
        model_id=model_id,
        base_url=base_url,
        configuration_revision=revision,
        voices=tuple(voices),
    )


async def discover_compute_voice_directory(
    root: str | Path,
    target: Mapping[str, Any],
    *,
    client: ComputeServiceClient | Any | None = None,
    secret_resolver: Any = None,
) -> ComputeVoiceDirectory | None:
    """Resolve one Call target to one trusted Compute offering and live catalogue."""

    selected_client = client or ComputeServiceClient(secret_resolver=secret_resolver)
    config = ComputeServicesConfig(Path(root) / "compute_services.json")
    try:
        directory = ComputeServiceDirectory(config, client=selected_client)
        refresh = await directory.refresh()
    except ComputeServiceError as exc:
        if exc.code == "compute_not_configured":
            return None
        raise
    endpoint = str(target.get("base_url") or "").rstrip("/") + "/audio/speech"
    matches = []
    for snapshot in refresh.catalogue.snapshots:
        if not snapshot.reachable:
            continue
        for offering in snapshot.offerings:
            if (
                offering.model_id != target.get("model")
                or not offering.enabled
                or offering.kind not in {"tts", "advanced_tts"}
            ):
                continue
            try:
                offered_endpoint = offering.interface_url(
                    snapshot.node,
                    "openai.audio.speech",
                )
            except ComputeServiceError:
                continue
            if offered_endpoint.rstrip("/") == endpoint.rstrip("/"):
                matches.append(snapshot)
    if not matches:
        return None
    if len(matches) != 1:
        raise ComputeServiceError("speech_catalogue_ambiguous")
    snapshot = matches[0]
    payload = await selected_client.fetch_json_resource(
        snapshot.node,
        snapshot.resource_path("speech_catalogue"),
    )
    catalogue = parse_speech_catalogue(
        payload,
        model_id=str(target.get("model") or ""),
        expected_base_url=str(target.get("base_url") or ""),
    )
    return ComputeVoiceDirectory(
        target_id=str(target.get("id") or ""),
        node=snapshot.node,
        catalogue=catalogue,
        client=selected_client,
    )


__all__ = [
    "ComputeVoice",
    "ComputeVoiceDirectory",
    "DownloadedVoicePreview",
    "SpeechCatalogue",
    "VoiceAudition",
    "discover_compute_voice_directory",
    "parse_speech_catalogue",
]
