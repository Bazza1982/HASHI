from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import aiohttp

from orchestrator.media_runtime import configured_media_executable
from orchestrator.openai_compatible_speech import (
    SpeechProtocolError,
    build_speech_request,
    validate_speech_audio,
)
from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import (
    VoiceAsset,
    convert_audio_to_ogg,
    convert_wav_to_ogg,
)


class OpenAICompatibleTTSError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(message)


class OpenAICompatibleTTSProvider(BaseTTSProvider):
    """One configured service implementing POST /audio/speech."""

    provider_name = "openai_compatible"

    def __init__(
        self,
        ffmpeg_cmd: str = "ffmpeg",
        *,
        service_id: str,
        service: Mapping[str, object],
        secrets: Mapping[str, object] | None = None,
        bridge_home: Path | None = None,
        session_factory=aiohttp.ClientSession,
    ) -> None:
        super().__init__(ffmpeg_cmd=ffmpeg_cmd)
        self.service_id = str(service_id)
        self.service = dict(service)
        self.secrets = dict(secrets or {})
        self.bridge_home = Path(bridge_home).resolve() if bridge_home else None
        self.session_factory = session_factory

    def _credential(self) -> str:
        reference = str(self.service.get("credential_ref") or "").strip()
        environment = str(self.service.get("credential_env") or "").strip()
        if reference.startswith("env://"):
            value = os.environ.get(reference.removeprefix("env://"), "")
        elif reference.startswith("secrets://"):
            value = self.secrets.get(reference.removeprefix("secrets://"), "")
        elif environment:
            value = os.environ.get(environment, "")
        else:
            return ""
        if not isinstance(value, str) or not value:
            raise OpenAICompatibleTTSError(
                "tts_credential_missing",
                f"{self.service_id} TTS credential is unavailable.",
                retryable=False,
            )
        return value

    async def _request(self, body: dict) -> tuple[bytes, str]:
        key = self._credential()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        maximum = int(self.service["max_response_bytes"])
        timeout = aiohttp.ClientTimeout(total=float(self.service["timeout_seconds"]))
        try:
            async with self.session_factory(timeout=timeout, trust_env=False) as session:
                async with session.post(
                    str(self.service["base_url"]).rstrip("/") + "/audio/speech",
                    json=body,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status not in {200, 201}:
                        status = int(response.status)
                        if status in {401, 403}:
                            code, retryable = "tts_authentication_failed", False
                        elif status == 429:
                            code, retryable = "tts_rate_limited", True
                        elif status >= 500:
                            code, retryable = "tts_provider_unavailable", True
                        else:
                            code, retryable = "tts_protocol_rejected", False
                        detail = f"{self.service_id} TTS rejected the request (HTTP {status})."
                        if status in {400, 404, 422}:
                            # Read only a bounded, typed code; never relay arbitrary
                            # provider response prose into logs or user notices.
                            raw = await response.content.read(4097)
                            try:
                                error = json.loads(raw) if len(raw) <= 4096 else {}
                            except (ValueError, UnicodeError):
                                error = {}
                            error = error.get("error") if isinstance(error, dict) else None
                            if isinstance(error, dict) and error.get("code") == "voice_not_found":
                                code, retryable = "tts_voice_unavailable", False
                                detail = f"{self.service_id} cannot find the configured voice."
                        raise OpenAICompatibleTTSError(
                            code,
                            detail,
                            status=status,
                            retryable=retryable,
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(16384):
                        total += len(chunk)
                        if total > maximum:
                            raise OpenAICompatibleTTSError(
                                "tts_response_limit",
                                f"{self.service_id} TTS response exceeded the configured limit.",
                                retryable=False,
                            )
                        chunks.append(chunk)
                    media_type = response.headers.get("Content-Type", "")
                    return b"".join(chunks), media_type
        except OpenAICompatibleTTSError:
            raise
        except TimeoutError as exc:
            raise OpenAICompatibleTTSError(
                "tts_timeout",
                f"{self.service_id} TTS request timed out.",
                retryable=True,
            ) from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise OpenAICompatibleTTSError(
                "tts_provider_unavailable",
                f"{self.service_id} TTS service is unavailable.",
                retryable=True,
            ) from exc

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
        maximum = min(int(max_chars), int(self.service.get("max_input_chars", max_chars)))
        spoken_text = self.prepare_spoken_text(text, max_chars=maximum)
        if not spoken_text:
            raise OpenAICompatibleTTSError(
                "tts_text_invalid",
                "No spoken text available for synthesis.",
            )
        voice = str(voice_name or "").strip()
        if not voice:
            raise OpenAICompatibleTTSError(
                "tts_voice_unavailable",
                f"{self.service_id} TTS requires a configured voice.",
            )
        options = dict(self.service.get("request_options") or {})
        options.update(dict(provider_options or {}))
        if rate and "speed" not in options:
            options["speed"] = max(0.25, min(4.0, 1 + int(rate) / 100))
        response_format = str(self.service["audio_format"])
        try:
            body = build_speech_request(
                model=str(self.service["model"]),
                text=spoken_text,
                voice=voice,
                response_format=response_format,
                options=options,
            )
            payload, media_type = await self._request(body)
            audio = validate_speech_audio(
                payload,
                media_type,
                response_format,
                pcm_sample_rate_hz=int(self.service["pcm_sample_rate_hz"]),
            )
        except SpeechProtocolError as exc:
            raise OpenAICompatibleTTSError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
            ) from exc

        output_dir.mkdir(parents=True, exist_ok=True)
        source_path = output_dir / f"{stem}.{audio.file_format}"
        ogg_path = output_dir / f"{stem}.ogg"
        source_path.write_bytes(audio.data)
        converter = (
            configured_media_executable("ffmpeg", bridge_home=self.bridge_home)
            or self.ffmpeg_cmd
        )
        if audio.file_format == "wav":
            await convert_wav_to_ogg(converter, source_path, ogg_path)
            wav_path: Path | None = source_path
        else:
            await convert_audio_to_ogg(converter, source_path, ogg_path)
            wav_path = None
        return VoiceAsset(
            provider=self.service_id,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )


__all__ = ["OpenAICompatibleTTSProvider", "OpenAICompatibleTTSError"]
