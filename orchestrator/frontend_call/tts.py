"""Call consumer for the shared catalogue-driven TTS platform."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

from orchestrator.voice_catalog import VoiceCatalog, VoiceCatalogError
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceSynthesisError, safe_voice_error_detail

from .contract import CallError


class CatalogCallTTS:
    """Bind Call's frozen selection to the same synthesis path as /voice."""

    def __init__(
        self,
        manager: VoiceManager,
        *,
        temp_root: Path,
        secrets: dict | None = None,
    ) -> None:
        self.manager = manager
        self.temp_root = Path(temp_root)
        self.secrets = dict(secrets or {})

    @staticmethod
    def _privacy_allows(service: dict, privacy_level: int) -> bool:
        return service.get("location") != "cloud" or privacy_level in (0, 1)

    def _credential_available(self, service: dict) -> bool:
        reference = str(service.get("credential_ref") or "")
        environment = str(service.get("credential_env") or "")
        if reference.startswith("env://"):
            return bool(os.environ.get(reference.removeprefix("env://")))
        if reference.startswith("secrets://"):
            return bool(self.secrets.get(reference.removeprefix("secrets://")))
        if environment:
            return bool(os.environ.get(environment))
        return True

    def has_eligible_path(
        self,
        catalog: VoiceCatalog | None,
        profile: dict | None,
        privacy_level: int,
    ) -> bool:
        if catalog is None or not profile:
            return False
        selected = str(profile.get("target_id") or "").strip().casefold()
        try:
            candidates = (selected, *catalog.fallbacks(selected))
        except VoiceCatalogError:
            return False
        return any(
            self._privacy_allows(service, privacy_level)
            and self._credential_available(service)
            and bool(catalog.voice_ids(service_id))
            for service_id in candidates
            for service in (catalog.service(service_id),)
        )

    @staticmethod
    def billing_target(catalog: VoiceCatalog, service_id: str) -> dict:
        service = catalog.service(service_id)
        return {
            "id": service_id,
            "kind": "tts",
            "label": service["label"],
            "adapter": service["adapter"],
            "location": service["location"],
            "base_url": service.get("base_url"),
            "model": service.get("model") or service["adapter"],
        }

    @staticmethod
    def _initial_selection(call, catalog: VoiceCatalog):
        slot = call.profile.get("tts") or {}
        service_id = str(slot.get("target_id") or "").strip().casefold()
        voice_id = str(slot.get("voice_id") or "").strip().casefold()
        try:
            preset = catalog.preset(voice_id)
        except VoiceCatalogError as exc:
            raise CallError("call_voice_unavailable", 409) from exc
        if preset.get("provider") != service_id:
            raise CallError("call_voice_unavailable", 409)
        return (
            service_id,
            str(preset.get("voice_name") or "").strip() or None,
            dict(preset.get("provider_options") or {}),
        )

    async def synthesize(
        self,
        call,
        text: str,
        *,
        privacy_level: int,
        request_id: str,
    ) -> dict:
        catalog = call.tts_catalog
        if catalog is None:
            raise CallError("call_tts_catalog_invalid", 503)
        if call.tts_effective_service:
            provider = call.tts_effective_service
            voice_name = call.tts_effective_voice
            provider_options = dict(call.tts_effective_options)
        else:
            provider, voice_name, provider_options = self._initial_selection(
                call, catalog
            )
        selected = str(call.profile["tts"]["target_id"])

        self.temp_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix="hashi-call-tts-", dir=self.temp_root
            ) as temp_dir:
                result = await self.manager.synthesize_configured(
                    agent_name=str(call.binding.get("agent_id") or "call"),
                    request_id=request_id,
                    text=text,
                    provider=provider,
                    voice_name=voice_name,
                    provider_options=provider_options,
                    max_chars=1200,
                    max_retries=0,
                    source_language=call.locale,
                    source_surface="call",
                    catalog=catalog,
                    provider_allowed=lambda _service_id, service: self._privacy_allows(
                        service, privacy_level
                    ),
                    output_dir=Path(temp_dir),
                )
                audio = result.asset.ogg_path.read_bytes()
        except VoiceSynthesisError as exc:
            call.tts_error_code = str(exc.code)
            call.tts_error_reason = safe_voice_error_detail(exc)
            raise CallError("call_speech_unavailable", 502) from exc
        except (OSError, ValueError) as exc:
            call.tts_error_code = "tts_audio_unavailable"
            call.tts_error_reason = (
                "The selected speech path returned no playable audio."
            )
            raise CallError("call_speech_unavailable", 502) from exc
        if not audio.startswith(b"OggS") or len(audio) > 32 * 1024 * 1024:
            call.tts_error_code = "tts_audio_invalid"
            call.tts_error_reason = "The selected speech path returned invalid audio."
            raise CallError("call_speech_unavailable", 502)

        effective = result.effective_provider
        call.tts_effective_service = effective
        call.tts_effective_voice = result.effective_voice_name
        call.tts_effective_options = dict(result.effective_provider_options)
        call.tts_error_code = ""
        call.tts_error_reason = ""
        if effective != selected and not call.tts_fallback_reason_code:
            call.tts_fallback_reason_code = str(
                result.asset.fallback_reason_code or "tts_provider_failed"
            )
        service = catalog.service(effective)
        return {
            "content_b64": base64.b64encode(audio).decode("ascii"),
            "media_type": result.asset.mime_type,
            "provider_receipt": None,
            "_billing_usage": None,
            "_billing_target": self.billing_target(catalog, effective),
            "_tts_route": {
                "service_id": effective,
                "service_label": service["label"],
                "location": service["location"],
                "voice_name": result.effective_voice_name,
                "selected_service_id": selected,
                "selected_service_label": catalog.service_label(selected),
                "degraded": effective != selected,
                "reason_code": call.tts_fallback_reason_code or None,
            },
        }


__all__ = ["CatalogCallTTS"]
