from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.compute_services import (
    ComputeReference,
    ComputeRequirements,
    ComputeServiceClient,
    ComputeServiceDirectory,
    ComputeServiceError,
    ComputeServicesConfig,
)
from orchestrator.media_runtime import configured_media_executable
from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


def _default_config_path() -> Path:
    root = Path(
        os.environ.get("HASHI_SOURCE_ROOT")
        or os.environ.get("BRIDGE_HOME")
        or Path(__file__).resolve().parents[2]
    ).resolve()
    return root / "compute_services.json"


def _exact_reference(value: Any) -> ComputeReference:
    if not isinstance(value, Mapping):
        raise RuntimeError(
            "Hashi Compute TTS requires an exact compute_reference."
        )
    allowed = {"node_id", "service_id", "model_id"}
    if set(value) - allowed or not {"node_id", "service_id"}.issubset(value):
        raise RuntimeError(
            "Hashi Compute TTS requires an exact compute_reference."
        )
    try:
        return ComputeReference(
            node_id=value["node_id"],
            service_id=value["service_id"],
            model_id=value.get("model_id"),
        )
    except ComputeServiceError as exc:
        raise RuntimeError(
            "Hashi Compute TTS requires an exact compute_reference."
        ) from exc


class HashiComputeTTSProvider(BaseTTSProvider):
    """Resolve one configured LAN/local Compute speech service exactly."""

    provider_name = "hashi_compute"

    def __init__(
        self,
        ffmpeg_cmd: str = "ffmpeg",
        *,
        config_path: Path | None = None,
        directory_factory=ComputeServiceDirectory,
        client_factory=ComputeServiceClient,
    ) -> None:
        super().__init__(ffmpeg_cmd=ffmpeg_cmd)
        self.config_path = Path(config_path or _default_config_path()).resolve()
        self.directory_factory = directory_factory
        self.client_factory = client_factory

    async def synthesize(
        self,
        text: str,
        output_dir: Path,
        stem: str,
        voice_name: str | None = None,
        rate: int = 0,
        max_chars: int = 1200,
        provider_options: dict | None = None,
    ) -> VoiceAsset:
        spoken_text = self.prepare_spoken_text(text, max_chars=max_chars)
        if not spoken_text:
            raise RuntimeError("No spoken text available for synthesis.")
        options = dict(provider_options or {})
        reference = _exact_reference(options.get("compute_reference"))
        voice = str(voice_name or options.get("voice") or "").strip()
        if not voice:
            raise RuntimeError("Hashi Compute TTS requires an exact voice name.")
        response_format = str(options.get("response_format") or "wav").casefold()
        if response_format != "wav":
            raise RuntimeError("Hashi Compute TTS currently requires WAV output.")
        speed = float(options.get("speed", max(0.25, min(4.0, 1 + int(rate) / 100))))
        if not 0.25 <= speed <= 4.0:
            raise RuntimeError("Hashi Compute TTS speed must be between 0.25 and 4.0.")

        client = self.client_factory()
        directory = self.directory_factory(
            ComputeServicesConfig(self.config_path),
            client=client,
        )
        refreshed = await directory.refresh()
        resolved = refreshed.catalogue.resolve(
            reference,
            ComputeRequirements(
                input_modalities=frozenset({"text"}),
                output_modalities=frozenset({"audio"}),
                protocols=frozenset({"openai.audio.speech"}),
                locations=frozenset({"local", "lan"}),
            ),
        )
        model = str(resolved.offering.model_id or "").strip()
        if not model:
            raise RuntimeError("Hashi Compute TTS resolved no model.")
        request_options = options.get("request_options")
        if request_options is None:
            request_options = {}
        if not isinstance(request_options, Mapping) or {
            "model",
            "input",
            "voice",
            "response_format",
            "speed",
        }.intersection(request_options):
            raise RuntimeError("Hashi Compute TTS request_options are invalid.")
        payload, content_type = await client.invoke_bytes(
            resolved,
            "openai.audio.speech",
            {
                "model": model,
                "input": spoken_text,
                "voice": voice,
                "response_format": response_format,
                "speed": speed,
                **dict(request_options),
            },
        )
        if content_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
            raise RuntimeError("Hashi Compute TTS returned an unsupported audio type.")
        if not (payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"):
            raise RuntimeError("Hashi Compute TTS returned malformed WAV audio.")

        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"{stem}.wav"
        ogg_path = output_dir / f"{stem}.ogg"
        wav_path.write_bytes(payload)
        converter = (
            configured_media_executable("ffmpeg", bridge_home=self.config_path.parent)
            or self.ffmpeg_cmd
        )
        await convert_wav_to_ogg(converter, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )


__all__ = ["HashiComputeTTSProvider"]
