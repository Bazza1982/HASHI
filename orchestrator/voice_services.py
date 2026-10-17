"""Provider-neutral TTS service and voice catalogue for Agent voice settings."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from orchestrator.compute_services import (
    ComputeServiceClient,
    ComputeServiceDirectory,
    ComputeServiceError,
    ComputeServicesConfig,
)
from orchestrator.compute_voice_catalog import (
    ComputeVoiceDirectory,
    parse_speech_catalogue,
)
from orchestrator.gemini_tts_catalog import GEMINI_TTS_MODEL, GEMINI_VOICES


PRIVACY_LOCAL = "local_private"
PRIVACY_EXTERNAL = "external"


class _MappingSecretResolver:
    def __init__(self, secrets: Mapping[str, Any]):
        self.secrets = secrets

    def resolve(self, reference: str):
        key = str(reference or "").rsplit("/", 1)[-1]
        value = self.secrets.get(key)
        if not value:
            raise ValueError("secret unavailable")
        return SimpleNamespace(value=value)


@dataclass(frozen=True)
class VoiceChoice:
    id: str
    label: str
    language: str | None = None
    gender: str | None = None
    description: str = ""
    callable: bool = True
    # Voice-specific synthesis hints (for example Kokoro's language code).
    # Runtime executable/cache paths remain provider-derived and are never
    # copied into the Agent's durable route.
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceService:
    id: str
    label: str
    service: str
    provider: str
    privacy: str
    location: str
    available: bool
    availability: str
    model: str | None = None
    node_id: str | None = None
    offering_id: str | None = None
    supports_speed: bool = False
    speed_min: float | None = None
    speed_max: float | None = None
    supports_style: bool = False
    voices: tuple[VoiceChoice, ...] = ()
    compute_directory: ComputeVoiceDirectory | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def privacy_label(self) -> str:
        if self.privacy == PRIVACY_LOCAL:
            return f"Local private · {'LAN' if self.location == 'lan' else 'device'}"
        return "External service · content leaves local private boundary"

    def route(self, voice_id: str, *, options: Mapping[str, Any] | None = None) -> dict:
        voice = next((item for item in self.voices if item.id == voice_id), None)
        if voice is None:
            raise RuntimeError("Voice is not present in the selected service catalogue.")
        selected = {**dict(voice.options), **dict(options or {})}
        if "speed" in selected and not self.supports_speed:
            raise RuntimeError("The selected voice service does not support speed control.")
        if "style" in selected and not self.supports_style:
            raise RuntimeError("The selected voice service does not support performance style.")
        return {
            "service": self.service,
            "node_id": self.node_id,
            "offering_id": self.offering_id,
            "model": self.model,
            "voice": voice_id,
            "options": selected,
        }


@dataclass(frozen=True)
class VoiceServiceCatalogue:
    services: tuple[VoiceService, ...]
    errors: Mapping[str, str] = field(default_factory=dict)

    def service(self, service_id: str) -> VoiceService:
        for service in self.services:
            if service.id == service_id:
                return service
        raise RuntimeError("Voice service is no longer available; refresh the menu.")

    def matching_route(self, route: Mapping[str, Any]) -> VoiceService:
        for service in self.services:
            if (
                service.service == route.get("service")
                and service.node_id == route.get("node_id")
                and service.offering_id == route.get("offering_id")
                and service.model == route.get("model")
            ):
                return service
        raise RuntimeError("Configured voice service is unavailable.")


def _service_id(*parts: object) -> str:
    source = "\0".join(str(part or "") for part in parts).encode("utf-8")
    return "vs-" + hashlib.sha256(source).hexdigest()[:16]


def _static_services(
    *,
    platform_name: str,
    built_in_voices: Mapping[str, Iterable[VoiceChoice]],
) -> list[VoiceService]:
    result: list[VoiceService] = []
    windows_voices = tuple(built_in_voices.get("windows", ()))
    if platform_name == "win32":
        result.append(
            VoiceService(
                id="windows",
                label="Windows TTS",
                service="windows",
                provider="windows",
                privacy=PRIVACY_LOCAL,
                location="device",
                available=bool(windows_voices),
                availability="ready" if windows_voices else "no installed voice detected",
                supports_speed=True,
                speed_min=0.5,
                speed_max=2.0,
                voices=windows_voices,
            )
        )
    edge_voices = tuple(built_in_voices.get("edge", ()))
    result.append(
        VoiceService(
            id="edge",
            label="Edge TTS",
            service="edge",
            provider="edge",
            privacy=PRIVACY_EXTERNAL,
            location="network",
            available=bool(edge_voices),
            availability="ready" if edge_voices else "voice catalogue unavailable",
            supports_speed=True,
            speed_min=0.5,
            speed_max=2.0,
            voices=edge_voices,
        )
    )
    kokoro_voices = tuple(built_in_voices.get("kokoro", ()))
    result.append(
        VoiceService(
            id="device-kokoro",
            label="Device Kokoro TTS",
            service="kokoro",
            provider="kokoro",
            privacy=PRIVACY_LOCAL,
            location="device",
            available=bool(kokoro_voices),
            availability="ready" if kokoro_voices else "not installed",
            supports_speed=True,
            speed_min=0.5,
            speed_max=2.0,
            voices=kokoro_voices,
        )
    )
    gemini = tuple(
        VoiceChoice(
            id=name,
            label=name,
            gender=gender,
            description=style,
        )
        for name, (gender, style) in GEMINI_VOICES.items()
    )
    result.append(
        VoiceService(
            id="openrouter-gemini",
            label="Gemini TTS via OpenRouter",
            service="openrouter",
            provider="openai_compatible",
            privacy=PRIVACY_EXTERNAL,
            location="network",
            available=True,
            availability="requires configured OpenRouter credential",
            model=GEMINI_TTS_MODEL,
            supports_style=True,
            voices=gemini,
        )
    )
    return result


async def discover_voice_services(
    root: str | Path,
    *,
    client: ComputeServiceClient | Any | None = None,
    secret_resolver: Any = None,
    secrets: Mapping[str, Any] | None = None,
    platform_name: str | None = None,
    built_in_voices: Mapping[str, Iterable[VoiceChoice]] | None = None,
) -> VoiceServiceCatalogue:
    """Build one view from platform voices and the authoritative Compute directory."""

    selected_platform = platform_name or sys.platform
    services = _static_services(
        platform_name=selected_platform,
        built_in_voices=built_in_voices or {},
    )
    errors: dict[str, str] = {}
    selected_resolver = secret_resolver
    if selected_resolver is None and secrets:
        selected_resolver = _MappingSecretResolver(secrets)
    selected_client = client or ComputeServiceClient(secret_resolver=selected_resolver)
    config = ComputeServicesConfig(Path(root) / "compute_services.json")
    try:
        refresh = await ComputeServiceDirectory(config, client=selected_client).refresh()
    except ComputeServiceError as exc:
        if exc.code != "compute_not_configured":
            errors["compute"] = exc.code
        return VoiceServiceCatalogue(tuple(services), errors)

    errors.update(refresh.errors)
    for snapshot in refresh.catalogue.snapshots:
        if not snapshot.reachable:
            continue
        for offering in snapshot.offerings:
            if offering.kind not in {"tts", "advanced_tts"} or not offering.enabled:
                continue
            model = str(offering.model_id or "").strip()
            if not model:
                continue
            key = f"{snapshot.node.id}:{offering.service_id}:{model}"
            voice_directory = None
            voices: tuple[VoiceChoice, ...] = ()
            availability = offering.status
            available = offering.status in {"ready", "busy"}
            try:
                endpoint = offering.interface_url(
                    snapshot.node, "openai.audio.speech"
                )
                base_url = endpoint.rsplit("/audio/speech", 1)[0]
                payload = await selected_client.fetch_json_resource(
                    snapshot.node,
                    snapshot.resource_path("speech_catalogue"),
                )
                speech = parse_speech_catalogue(
                    payload,
                    model_id=model,
                    expected_base_url=base_url,
                )
                voice_directory = ComputeVoiceDirectory(
                    target_id=_service_id(key),
                    node=snapshot.node,
                    catalogue=speech,
                    client=selected_client,
                )
                voices = tuple(
                    VoiceChoice(
                        id=voice.id,
                        label=voice.label,
                        language=voice.language,
                        gender=voice.gender,
                        description=voice.description,
                        callable=voice.callable,
                    )
                    for voice in speech.voices
                )
            except ComputeServiceError as exc:
                errors[key] = exc.code
                availability = exc.code
                available = False
            advanced = offering.kind == "advanced_tts"
            privacy = (
                PRIVACY_LOCAL
                if snapshot.privacy_location in {"local", "lan"}
                else PRIVACY_EXTERNAL
            )
            services.append(
                VoiceService(
                    id=_service_id(key),
                    label=(
                        "Hashi Compute advanced TTS"
                        if advanced
                        else "Hashi Compute TTS"
                    ),
                    service="hashi_compute",
                    provider="hashi_compute",
                    privacy=privacy,
                    location=snapshot.privacy_location,
                    available=available and bool(voices),
                    availability=availability,
                    model=model,
                    node_id=snapshot.node.id,
                    offering_id=offering.service_id,
                    supports_speed=True,
                    speed_min=0.5,
                    speed_max=2.0,
                    supports_style=advanced,
                    voices=voices,
                    compute_directory=voice_directory,
                )
            )
    return VoiceServiceCatalogue(tuple(services), errors)


__all__ = [
    "PRIVACY_EXTERNAL",
    "PRIVACY_LOCAL",
    "VoiceChoice",
    "VoiceService",
    "VoiceServiceCatalogue",
    "discover_voice_services",
]
