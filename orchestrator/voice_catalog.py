"""Configuration-owned TTS services and /voice menu entries."""

from __future__ import annotations

import copy
import math
import re
import sys
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from orchestrator.config_json import read_config_json
from orchestrator.tts_routing import normalize_language


DEFAULT_VOICE_CATALOG_PATH = Path(__file__).with_name("voice_catalog.default.json")
VOICE_CATALOG_FILENAME = "voice_catalog.json"
SUPPORTED_TTS_ADAPTERS = frozenset(
    {
        "edge",
        "windows",
        "macos",
        "piper",
        "kokoro",
        "coqui",
        "openai_compatible",
    }
)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CREDENTIAL_REF = re.compile(
    r"(?:env://[A-Z][A-Z0-9_]{0,100}|secrets://[A-Za-z0-9][A-Za-z0-9_-]{0,100})"
)
_CREDENTIAL_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,100}")
_PRIVATE_LAN_NETWORKS = tuple(
    ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_RESERVED_REQUEST_FIELDS = frozenset(
    {
        "model",
        "input",
        "voice",
        "response_format",
        "url",
        "headers",
        "api_key",
    }
)
_COMMON_SERVICE_FIELDS = frozenset(
    {
        "id",
        "adapter",
        "label",
        "menu_label",
        "location",
        "style_support",
        "language_voices",
        "fallbacks",
        "platforms",
        "enabled",
    }
)
_OPENAI_SERVICE_FIELDS = frozenset(
    {
        "location",
        "base_url",
        "model",
        "audio_format",
        "credential_ref",
        "credential_env",
        "request_options",
        "timeout_seconds",
        "max_response_bytes",
        "max_input_chars",
        "pcm_sample_rate_hz",
    }
)
_VOICE_FIELDS = frozenset(
    {
        "id",
        "service",
        "voice_name",
        "label",
        "display_names",
        "language",
        "language_labels",
        "locale",
        "gender",
        "style",
        "provider_options",
        "other_languages_unsupported",
        "platforms",
        "enabled",
    }
)


class VoiceCatalogError(RuntimeError):
    """Stable configuration-boundary failure for the TTS catalogue."""


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise VoiceCatalogError(f"{field} must be an identifier")
    selected = value.strip().casefold()
    if _IDENTIFIER.fullmatch(selected) is None:
        raise VoiceCatalogError(f"{field} must be an identifier")
    return selected


def _plain_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise VoiceCatalogError(f"{field} must be text")
    selected = value.strip()
    if (
        not selected
        or len(selected) > maximum
        or any(ord(character) < 32 for character in selected)
    ):
        raise VoiceCatalogError(f"{field} is invalid")
    return selected


def _platforms(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > 8
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise VoiceCatalogError("platforms must be a short list of names")
    return tuple(dict.fromkeys(item.strip().casefold() for item in value))


def _json_value(value: object, field: str, *, depth: int = 0) -> object:
    if depth > 4:
        raise VoiceCatalogError(f"{field} is too deeply nested")
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 1_000_000:
            raise VoiceCatalogError(f"{field} contains an invalid number")
        return value
    if isinstance(value, str):
        if len(value) > 2000 or any(ord(character) < 9 for character in value):
            raise VoiceCatalogError(f"{field} contains invalid text")
        return value
    if isinstance(value, list):
        if len(value) > 64:
            raise VoiceCatalogError(f"{field} contains too many values")
        return [_json_value(item, field, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise VoiceCatalogError(f"{field} contains too many fields")
        result: dict[str, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 80
                or any(ord(character) < 32 for character in key)
            ):
                raise VoiceCatalogError(f"{field} contains an invalid field")
            result[key] = _json_value(item, field, depth=depth + 1)
        return result
    raise VoiceCatalogError(f"{field} contains an unsupported value")


def _options(value: object, field: str) -> dict[str, object]:
    selected = _json_value(value if value is not None else {}, field)
    if not isinstance(selected, dict):
        raise VoiceCatalogError(f"{field} must be an object")
    if set(selected) & _RESERVED_REQUEST_FIELDS:
        raise VoiceCatalogError(f"{field} cannot replace protocol-owned fields")
    return selected


def _localized_text(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 16:
        raise VoiceCatalogError(f"{field} must be a locale map")
    result: dict[str, str] = {}
    for locale, text in value.items():
        key = _plain_text(locale, f"{field} locale", 32)
        result[key] = _plain_text(text, f"{field} value", 100)
    return result


def _private_lan_literal(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and any(
        address in network for network in _PRIVATE_LAN_NETWORKS
    )


def _validate_openai_endpoint(service: dict[str, object]) -> None:
    raw_url = _plain_text(service.get("base_url"), "service base_url", 500)
    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise VoiceCatalogError("service base_url is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise VoiceCatalogError("service base_url is invalid")
    location = str(service.get("location") or "").strip().casefold()
    hostname = parsed.hostname.casefold()
    if location == "local":
        valid = parsed.scheme in {"http", "https"} and hostname in _LOOPBACK_HOSTS
    elif location == "lan":
        valid = parsed.scheme in {"http", "https"} and _private_lan_literal(hostname)
    elif location == "cloud":
        valid = parsed.scheme == "https" and hostname not in _LOOPBACK_HOSTS
        try:
            address = ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private or address.is_loopback or address.is_link_local
        ):
            valid = False
    else:
        valid = False
    if not valid:
        raise VoiceCatalogError("service location and base_url do not match")
    service["location"] = location
    service["base_url"] = raw_url.rstrip("/")


def _validate_service(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise VoiceCatalogError("voice service must be an object")
    adapter = str(value.get("adapter") or "").strip().casefold()
    allowed = _COMMON_SERVICE_FIELDS | (
        _OPENAI_SERVICE_FIELDS if adapter == "openai_compatible" else frozenset()
    )
    if set(value) - allowed:
        raise VoiceCatalogError("voice service has unsupported fields")
    if adapter not in SUPPORTED_TTS_ADAPTERS:
        raise VoiceCatalogError(f"unsupported TTS adapter: {adapter or 'missing'}")
    service: dict[str, object] = {
        "id": _identifier(value.get("id"), "service id"),
        "adapter": adapter,
        "label": _plain_text(value.get("label"), "service label", 100),
        "menu_label": _plain_text(
            value.get("menu_label", value.get("label")),
            "service menu_label",
            50,
        ),
        "style_support": str(value.get("style_support") or "unknown")
        .strip()
        .casefold(),
        "fallbacks": tuple(
            _identifier(item, "fallback service")
            for item in (
                value.get("fallbacks")
                if isinstance(value.get("fallbacks"), list)
                else []
            )
        ),
        "platforms": _platforms(value.get("platforms")),
        "enabled": value.get("enabled", True),
    }
    if "language_voices" in value:
        mapping = value["language_voices"]
        if not isinstance(mapping, Mapping) or not mapping or set(mapping) - {"zh", "en", "ja"}:
            raise VoiceCatalogError("service language_voices must map supported language codes to voice IDs")
        service["language_voices"] = {
            language: _identifier(alias, "language voice")
            for language, alias in mapping.items()
        }
    default_location = "cloud" if adapter == "edge" else "local"
    location = str(value.get("location") or default_location).strip().casefold()
    if location not in {"local", "lan", "cloud"}:
        raise VoiceCatalogError("service location is invalid")
    if adapter != "openai_compatible" and (
        (adapter == "edge" and location != "cloud")
        or (adapter != "edge" and location != "local")
    ):
        raise VoiceCatalogError("service adapter and location do not match")
    service["location"] = location
    if service["style_support"] not in {"supported", "unsupported", "unknown"}:
        raise VoiceCatalogError("service style_support is invalid")
    if type(service["enabled"]) is not bool:
        raise VoiceCatalogError("service enabled must be boolean")
    if not isinstance(value.get("fallbacks", []), list) or len(service["fallbacks"]) > 16:
        raise VoiceCatalogError("service fallbacks must be a short list")
    if len(set(service["fallbacks"])) != len(service["fallbacks"]):
        raise VoiceCatalogError("service fallbacks contain duplicates")
    if adapter != "openai_compatible":
        return service

    _validate_openai_endpoint(service := {**service, **{
        "location": value.get("location"),
        "base_url": value.get("base_url"),
    }})
    service["model"] = _plain_text(value.get("model"), "service model", 200)
    audio_format = str(value.get("audio_format") or "mp3").strip().casefold()
    if audio_format not in {"mp3", "wav", "pcm"}:
        raise VoiceCatalogError("service audio_format is unsupported")
    service["audio_format"] = audio_format
    credential_ref = value.get("credential_ref")
    credential_env = value.get("credential_env")
    if credential_ref is not None:
        if (
            not isinstance(credential_ref, str)
            or _CREDENTIAL_REF.fullmatch(credential_ref.strip()) is None
        ):
            raise VoiceCatalogError("service credential_ref is invalid")
        service["credential_ref"] = credential_ref.strip()
    if credential_env is not None:
        if (
            not isinstance(credential_env, str)
            or _CREDENTIAL_ENV.fullmatch(credential_env.strip()) is None
            or credential_ref is not None
        ):
            raise VoiceCatalogError("service credential_env is invalid")
        service["credential_env"] = credential_env.strip()
    service["request_options"] = _options(
        value.get("request_options"), "service request_options"
    )
    timeout = value.get("timeout_seconds", 25)
    maximum = value.get("max_response_bytes", 16 * 1024 * 1024)
    sample_rate = value.get("pcm_sample_rate_hz", 24000)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise VoiceCatalogError("service timeout_seconds is invalid")
    if type(maximum) is not int or not 1024 <= maximum <= 32 * 1024 * 1024:
        raise VoiceCatalogError("service max_response_bytes is invalid")
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 96000:
        raise VoiceCatalogError("service pcm_sample_rate_hz is invalid")
    service["timeout_seconds"] = float(timeout)
    service["max_response_bytes"] = maximum
    service["pcm_sample_rate_hz"] = sample_rate
    if "max_input_chars" in value:
        limit = value["max_input_chars"]
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise VoiceCatalogError("service max_input_chars is invalid")
        service["max_input_chars"] = limit
    return service


def _validate_voice(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) - _VOICE_FIELDS:
        raise VoiceCatalogError("voice entry is invalid")
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise VoiceCatalogError("voice enabled must be boolean")
    gender = str(value.get("gender") or "unknown").strip().casefold()
    if gender not in {"female", "male", "mixed", "unknown"}:
        raise VoiceCatalogError("voice gender is invalid")
    voice_id = _identifier(value.get("id"), "voice id")
    if len(voice_id) > 48:
        raise VoiceCatalogError("voice id is too long for menu callbacks")
    result = {
        "id": voice_id,
        "service": _identifier(value.get("service"), "voice service"),
        "voice_name": _plain_text(value.get("voice_name"), "voice_name", 500),
        "label": _plain_text(value.get("label"), "voice label", 100),
        "display_names": _localized_text(value.get("display_names"), "display_names"),
        "language": _plain_text(value.get("language"), "voice language", 60),
        "language_labels": _localized_text(
            value.get("language_labels"), "language_labels"
        ),
        "locale": _plain_text(value.get("locale"), "voice locale", 32),
        "gender": gender,
        "provider_options": _options(
            value.get("provider_options"), "voice provider_options"
        ),
        "other_languages_unsupported": value.get(
            "other_languages_unsupported", True
        ),
        "platforms": _platforms(value.get("platforms")),
        "enabled": enabled,
    }
    if value.get("style") is not None:
        result["style"] = _plain_text(value.get("style"), "voice style", 80)
    return result


def _read_document(path: Path, *, optional: bool) -> tuple[dict[str, object], str | None]:
    try:
        document = read_config_json(path)
    except FileNotFoundError:
        if optional:
            return {}, None
        raise VoiceCatalogError(f"voice catalog is missing: {path.name}") from None
    except (OSError, ValueError, TypeError) as exc:
        raise VoiceCatalogError(
            f"voice catalog is unreadable: {path.name}: {exc}"
        ) from exc
    payload = dict(document)
    unsupported = set(payload) - {"version", "services", "voices", "default_voice"}
    if unsupported:
        raise VoiceCatalogError(
            f"voice catalog has unsupported fields: {', '.join(sorted(unsupported))}"
        )
    if payload.get("version") != 1:
        raise VoiceCatalogError(f"voice catalog version is unsupported: {path.name}")
    return payload, document.revision


def _validated_rows(document: Mapping[str, object], key: str, validator) -> list[dict]:
    if not document:
        return []
    rows = document.get(key, [])
    if not isinstance(rows, list) or len(rows) > 500:
        raise VoiceCatalogError(f"voice catalog {key} must be a bounded list")
    selected = [validator(row) for row in rows]
    identifiers = [str(row["id"]) for row in selected]
    if len(set(identifiers)) != len(identifiers):
        raise VoiceCatalogError(f"voice catalog {key} contains duplicate IDs")
    return selected


def _matches_platform(value: Mapping[str, object], platform: str) -> bool:
    platforms = tuple(value.get("platforms") or ())
    return not platforms or platform.casefold() in platforms


class VoiceCatalog:
    """Merged built-in and instance voice services, with no runtime writes."""

    def __init__(
        self,
        *,
        services: Mapping[str, Mapping[str, object]],
        presets: Mapping[str, Mapping[str, object]],
        default_revision: str,
        instance_revision: str | None,
        platform: str,
        default_voice: str | None = None,
    ) -> None:
        self.default_voice = default_voice
        self._services = copy.deepcopy(dict(services))
        self.presets = copy.deepcopy(dict(presets))
        self.default_revision = str(default_revision)
        self.instance_revision = instance_revision
        self.platform = str(platform)
        self.revision = ":".join(
            (self.default_revision, self.instance_revision or "", self.platform)
        )

    @classmethod
    def load(
        cls,
        *,
        instance_path: Path | None = None,
        project_root: Path | None = None,
        platform: str | None = None,
        include_disabled: bool = False,
        default_path: Path = DEFAULT_VOICE_CATALOG_PATH,
    ) -> "VoiceCatalog":
        root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        selected_platform = str(platform or sys.platform).strip().casefold()
        default_document, default_revision = _read_document(
            Path(default_path), optional=False
        )
        instance_document: dict[str, object] = {}
        revision: str | None = None
        if instance_path is not None:
            instance_document, revision = _read_document(
                Path(instance_path), optional=True
            )

        services: dict[str, dict[str, object]] = {}
        voices: dict[str, dict[str, object]] = {}
        for document in (default_document, instance_document):
            for service in _validated_rows(document, "services", _validate_service):
                services[str(service["id"])] = service
            for voice in _validated_rows(document, "voices", _validate_voice):
                voices[str(voice["id"])] = voice

        for service in services.values():
            unknown = set(service["fallbacks"]) - set(services)
            if unknown:
                raise VoiceCatalogError(
                    f"service {service['id']} has unknown fallbacks: {', '.join(sorted(unknown))}"
                )
        for voice in voices.values():
            if voice["service"] not in services:
                raise VoiceCatalogError(
                    f"voice {voice['id']} references an unknown service"
                )
            if type(voice["other_languages_unsupported"]) is not bool:
                raise VoiceCatalogError(
                    "voice other_languages_unsupported must be boolean"
                )

        active_services = {
            service_id: service
            for service_id, service in services.items()
            if (service["enabled"] or include_disabled)
            and _matches_platform(service, selected_platform)
        }
        presets: dict[str, dict[str, object]] = {}
        for voice_id, voice in voices.items():
            service_id = str(voice["service"])
            if (
                not voice["enabled"]
                or service_id not in active_services
                or not _matches_platform(voice, selected_platform)
            ):
                continue
            voice_name = str(voice["voice_name"])
            if active_services[service_id]["adapter"] == "piper":
                candidate = Path(voice_name)
                if not candidate.is_absolute():
                    candidate = (root / candidate).resolve()
                    try:
                        candidate.relative_to(root)
                    except ValueError as exc:
                        raise VoiceCatalogError(
                            f"voice {voice_id} escapes the project root"
                        ) from exc
                voice_name = str(candidate)
            preset = {
                "provider": service_id,
                "voice_name": voice_name,
                "label": voice["label"],
                "language": voice["language"],
                "locale": voice["locale"],
                "gender": voice["gender"],
                "provider_options": copy.deepcopy(voice["provider_options"]),
                "other_languages_unsupported": voice[
                    "other_languages_unsupported"
                ],
            }
            for key in ("display_names", "language_labels"):
                if voice[key]:
                    preset[key] = copy.deepcopy(voice[key])
            if voice.get("style"):
                preset["style"] = voice["style"]
            presets[voice_id] = preset
        default_voice = instance_document.get(
            "default_voice", default_document.get("default_voice")
        )
        if default_voice is not None and (
            not isinstance(default_voice, str) or default_voice not in presets
        ):
            raise VoiceCatalogError("default voice must reference an active voice preset")
        for service_id, service in active_services.items():
            for language, alias in service.get("language_voices", {}).items():
                preset = presets.get(alias)
                if (preset is None or preset["provider"] != service_id
                        or normalize_language(preset["language"]) != language):
                    raise VoiceCatalogError("language voice must be an active voice of the same service and language")
        return cls(
            services=active_services,
            presets=presets,
            default_revision=str(default_revision or "default"),
            instance_revision=revision,
            platform=selected_platform,
            default_voice=default_voice,
        )

    @property
    def service_ids(self) -> tuple[str, ...]:
        return tuple(self._services)

    def service(self, service_id: str) -> dict[str, object]:
        selected = str(service_id or "").strip().casefold()
        try:
            return copy.deepcopy(self._services[selected])
        except KeyError as exc:
            raise VoiceCatalogError(f"unknown TTS service: {service_id}") from exc

    def service_label(self, service_id: str) -> str:
        return str(self.service(service_id)["label"])

    def service_menu_label(self, service_id: str) -> str:
        return str(self.service(service_id)["menu_label"])

    def fallbacks(self, service_id: str) -> tuple[str, ...]:
        service = self.service(service_id)
        return tuple(
            candidate
            for candidate in service["fallbacks"]
            if candidate in self._services and candidate != service_id
        )

    def voice_ids(self, service_id: str | None = None) -> tuple[str, ...]:
        selected = str(service_id or "").strip().casefold()
        return tuple(
            voice_id
            for voice_id, preset in self.presets.items()
            if not selected
            or str(preset.get("provider") or "").strip().casefold() == selected
        )

    def preset(self, voice_id: str) -> dict[str, object]:
        selected = str(voice_id or "").strip().casefold()
        try:
            return copy.deepcopy(self.presets[selected])
        except KeyError as exc:
            raise VoiceCatalogError(f"unknown TTS voice: {voice_id}") from exc

    def routing_profiles(self) -> dict[str, dict[str, object]]:
        return {
            service_id: {
                "style_support": service["style_support"],
                "fallbacks": tuple(service["fallbacks"]),
                **({"language_voices": copy.deepcopy(service["language_voices"])}
                   if "language_voices" in service else {}),
            }
            for service_id, service in self._services.items()
        }


__all__ = [
    "DEFAULT_VOICE_CATALOG_PATH",
    "SUPPORTED_TTS_ADAPTERS",
    "VOICE_CATALOG_FILENAME",
    "VoiceCatalog",
    "VoiceCatalogError",
]
