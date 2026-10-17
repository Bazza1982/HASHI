"""Hashi Compute LAN speech provider resolved through the Compute directory."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.compute_services import (
    ComputeReference,
    ComputeRequirements,
    ComputeServiceClient,
    ComputeServiceDirectory,
    ComputeServiceError,
    ComputeServicesConfig,
)
from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import (
    VoiceAsset,
    convert_audio_to_ogg,
    convert_wav_to_ogg,
)


class _MappingSecretResolver:
    def __init__(self, secrets: dict[str, Any]):
        self.secrets = secrets

    def resolve(self, reference: str):
        key = str(reference or "").rsplit("/", 1)[-1]
        value = self.secrets.get(key)
        if not value:
            raise ValueError("secret unavailable")
        return SimpleNamespace(value=value)


class HashiComputeTTSProvider(BaseTTSProvider):
    provider_name = "hashi_compute"

    def __init__(
        self,
        ffmpeg_cmd: str = "ffmpeg",
        *,
        project_root: Path | None = None,
        secrets: dict | None = None,
        client: ComputeServiceClient | Any | None = None,
    ) -> None:
        super().__init__(ffmpeg_cmd=ffmpeg_cmd)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.secrets = dict(secrets or {})
        self.client = client or ComputeServiceClient(
            secret_resolver=_MappingSecretResolver(self.secrets)
        )

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
        del rate
        # Both current Hashi Compute speech services declare a 1,000-character
        # request ceiling. ``prepare_spoken_text`` may append a full stop when
        # clipping, so enforce the service boundary once more afterwards.
        service_max_chars = min(max(1, int(max_chars)), 1000)
        spoken_text = self.prepare_spoken_text(text, max_chars=service_max_chars)
        if len(spoken_text) > service_max_chars:
            spoken_text = f"{spoken_text[: service_max_chars - 1].rstrip()}."
        if not spoken_text:
            raise RuntimeError("No spoken text available for synthesis.")
        options = dict(provider_options or {})
        node_id = str(options.pop("node_id", "") or "").strip()
        offering_id = str(options.pop("offering_id", "") or "").strip()
        model = str(options.pop("model", "") or "").strip()
        options.pop("service", None)
        voice = str(voice_name or "").strip()
        if not all((node_id, offering_id, model, voice)):
            raise RuntimeError("Hashi Compute TTS route is incomplete.")

        try:
            refresh = await ComputeServiceDirectory(
                ComputeServicesConfig(self.project_root / "compute_services.json"),
                client=self.client,
            ).refresh()
            resolved = refresh.catalogue.resolve(
                ComputeReference(node_id, offering_id, model),
                ComputeRequirements(
                    input_modalities=frozenset({"text"}),
                    output_modalities=frozenset({"audio"}),
                    protocols=frozenset({"openai.audio.speech"}),
                    # The exact node is already frozen in the Agent route.
                    # Privacy/location consent is enforced by VoiceManager;
                    # the provider must not contradict an explicitly selected
                    # internet Compute node by pretending only LAN exists.
                    locations=frozenset({"local", "lan", "internet"}),
                ),
                allow_busy=True,
            )
            style = str(options.pop("style", "") or "").strip()
            protocol = "openai.audio.speech"
            if style:
                try:
                    resolved.offering.interface("hashi.audio.speech.expressive")
                except ComputeServiceError as exc:
                    raise RuntimeError(
                        "Selected Hashi Compute service does not support performance style."
                    ) from exc
                protocol = "hashi.audio.speech.expressive"
            interface = resolved.offering.interface(protocol)
            output_formats = frozenset(
                str(item).strip().casefold()
                for item in getattr(resolved.offering.audio, "output_formats", ())
            )
            # Prefer the directory-declared compressed response.  The shared
            # Compute client intentionally caps resource bodies; requesting WAV
            # would make an otherwise valid longer reply exceed that boundary.
            response_format = "mp3" if "mp3" in output_formats else "wav"
            body: dict[str, Any] = {
                "model": model,
                "input": spoken_text,
                "voice": voice,
                "response_format": response_format,
            }
            if "speed" in options:
                body["speed"] = float(options.pop("speed"))
            if style:
                body["instructions"] = style
            if options:
                raise RuntimeError("Hashi Compute TTS route contains unsupported options.")
            payload, media_type = await self.client.post_json_resource_bytes(
                resolved.snapshot.node,
                interface.path,
                body,
            )
        except ComputeServiceError as exc:
            error = RuntimeError(f"Hashi Compute TTS failed: {exc.code}.")
            error.code = exc.code
            error.retryable = exc.code in {
                "compute_node_unreachable",
                "compute_service_busy",
                "compute_resource_rejected",
            }
            raise error from exc

        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path: Path | None = None
        if media_type == "audio/wav":
            if not (payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"):
                raise RuntimeError("Hashi Compute returned malformed WAV audio.")
            wav_path = output_dir / f"{stem}.wav"
            wav_path.write_bytes(payload)
            ogg_path = output_dir / f"{stem}.ogg"
            await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        elif media_type == "audio/mpeg":
            if not (
                payload.startswith(b"ID3")
                or (len(payload) > 2 and payload[0] == 255 and payload[1] & 0xE0 == 0xE0)
            ):
                raise RuntimeError("Hashi Compute returned malformed MP3 audio.")
            source = output_dir / f"{stem}.mp3"
            source.write_bytes(payload)
            ogg_path = output_dir / f"{stem}.ogg"
            await convert_audio_to_ogg(self.ffmpeg_cmd, source, ogg_path)
        elif media_type in {"audio/ogg", "audio/opus"}:
            if not payload.startswith(b"OggS"):
                raise RuntimeError("Hashi Compute returned malformed OGG audio.")
            ogg_path = output_dir / f"{stem}.ogg"
            ogg_path.write_bytes(payload)
        else:
            raise RuntimeError("Hashi Compute returned an unsupported audio format.")
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
            voice_name=voice,
        )


__all__ = ["HashiComputeTTSProvider"]
