"""PAO-owned discovery and exact resolution for local, LAN, and internet compute.

The configured node is the trust and privacy boundary.  A remote manifest may
describe capabilities and relative interfaces, but it cannot redirect HASHI to
another host or silently change a node from local/LAN to internet.  Consumers
resolve an exact node/service/model reference; capability search returns
candidates and never performs an implicit fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from ipaddress import ip_address, ip_network
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from orchestrator.config_json import (
    ConfigDocument,
    new_config_json,
    read_config_json,
    write_config_json,
)


COMPUTE_MANIFEST_SCHEMA = "hashi.compute.capabilities"
COMPUTE_MANIFEST_VERSION = 1
HASHI_COMPUTE_MANIFEST_FORMAT = "hashi-compute.manifest"
HASHI_COMPUTE_MANIFEST_VERSION = 1
COMPUTE_CONFIG_VERSION = 1
DEFAULT_COMPUTE_MANIFEST_PATH = "/.well-known/hashi-compute"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_INFERENCE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_AUDIO_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_NODES = 64
MAX_SERVICES = 256
MAX_MODELS_PER_SERVICE = 512

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROTOCOL = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_FORMAT = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_CREDENTIAL_REF = re.compile(
    r"(?:env://[A-Z][A-Z0-9_]{0,100}|secrets://[A-Za-z0-9][A-Za-z0-9_-]{0,100})"
)
_PRIVATE_LAN_NETWORKS = tuple(
    ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_AVAILABLE_STATUSES = frozenset({"ready", "healthy"})
_UNAVAILABLE_HEALTH = frozenset(
    {"offline", "unhealthy", "failed", "disabled", "stopped"}
)


class ComputeServiceError(RuntimeError):
    """Stable failure at the compute catalogue boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "compute_service_error")
        self.detail = str(detail or "")
        super().__init__(
            self.code if not self.detail else f"{self.code}: {self.detail}"
        )


def _identifier(value: Any, *, code: str = "compute_node_invalid") -> str:
    if not isinstance(value, str):
        raise ComputeServiceError(code)
    normalized = value.strip().casefold()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ComputeServiceError(code)
    return normalized


def _plain_text(value: Any, *, maximum: int, code: str) -> str:
    if not isinstance(value, str):
        raise ComputeServiceError(code)
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ComputeServiceError(code)
    return text


def _private_lan_literal(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and any(
        address in network for network in _PRIVATE_LAN_NETWORKS
    )


def _validate_path(
    value: Any,
    *,
    code: str,
    allow_templates: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ComputeServiceError(code)
    path = value.strip()
    if not path or len(path) > 500 or not path.startswith("/") or path.startswith("//"):
        raise ComputeServiceError(code)
    if "\\" in path or any(ord(char) < 32 for char in path):
        raise ComputeServiceError(code)
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ComputeServiceError(code)
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise ComputeServiceError(code)
    without_templates = _PLACEHOLDER.sub("value", path) if allow_templates else path
    if "{" in without_templates or "}" in without_templates:
        raise ComputeServiceError(code)
    return path


@dataclass(frozen=True)
class ComputeNode:
    id: str
    label: str
    location: str
    base_url: str
    manifest_path: str
    health_path: str | None = None
    credential_ref: str | None = None
    enabled: bool = True
    timeout_seconds: float = 8.0

    def validated(self) -> "ComputeNode":
        selected = validate_compute_node(
            {
                "id": self.id,
                "label": self.label,
                "location": self.location,
                "base_url": self.base_url,
                "manifest_path": self.manifest_path,
                "health_path": self.health_path,
                "credential_ref": self.credential_ref,
                "enabled": self.enabled,
                "timeout_seconds": self.timeout_seconds,
            }
        )
        if selected != self:
            raise ComputeServiceError("compute_node_invalid")
        return self

    def url(self, path: str) -> str:
        self.validated()
        selected = _validate_path(
            path,
            code="compute_interface_invalid",
            allow_templates=False,
        )
        return self.base_url.rstrip("/") + selected


def validate_compute_node(value: Any) -> ComputeNode:
    if not isinstance(value, Mapping):
        raise ComputeServiceError("compute_node_invalid")
    allowed = {
        "id",
        "label",
        "location",
        "base_url",
        "manifest_path",
        "health_path",
        "credential_ref",
        "enabled",
        "timeout_seconds",
    }
    if set(value) - allowed:
        raise ComputeServiceError("compute_node_invalid")
    node_id = _identifier(value.get("id"))
    label_raw = value.get("label", node_id)
    if not isinstance(label_raw, str):
        raise ComputeServiceError("compute_node_invalid")
    label = label_raw.strip()
    if not 1 <= len(label) <= 100 or any(ord(char) < 32 for char in label):
        raise ComputeServiceError("compute_node_invalid")
    location_raw = value.get("location")
    if not isinstance(location_raw, str):
        raise ComputeServiceError("compute_node_invalid")
    location = location_raw.strip().casefold()
    if location not in {"local", "lan", "internet"}:
        raise ComputeServiceError("compute_node_invalid")
    raw_url_value = value.get("base_url")
    if not isinstance(raw_url_value, str):
        raise ComputeServiceError("compute_node_invalid")
    raw_url = raw_url_value.strip()
    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise ComputeServiceError("compute_node_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ComputeServiceError("compute_node_invalid")
    hostname = parsed.hostname.casefold()
    if location == "local":
        valid_location = hostname in _LOOPBACK_HOSTS
    elif location == "lan":
        valid_location = _private_lan_literal(hostname)
    else:
        valid_location = parsed.scheme == "https"
        if valid_location:
            try:
                address = ip_address(hostname)
            except ValueError:
                address = None
            valid_location = bool(
                hostname not in _LOOPBACK_HOSTS
                and (
                    address is None
                    or not (
                        address.is_private
                        or address.is_loopback
                        or address.is_link_local
                        or address.is_unspecified
                    )
                )
            )
    if not valid_location:
        raise ComputeServiceError("compute_node_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ComputeServiceError("compute_node_invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ComputeServiceError("compute_node_invalid")
    rendered_host = (
        f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    )
    authority = rendered_host + (f":{port}" if port is not None else "")
    base_url = urlunsplit((parsed.scheme.casefold(), authority, "", "", ""))
    manifest_path = _validate_path(
        value.get("manifest_path", DEFAULT_COMPUTE_MANIFEST_PATH),
        code="compute_node_invalid",
    )
    health_raw = value.get("health_path")
    health_path = (
        _validate_path(health_raw, code="compute_node_invalid")
        if health_raw is not None and health_raw != ""
        else None
    )
    credential_raw = value.get("credential_ref")
    if credential_raw is not None and not isinstance(credential_raw, str):
        raise ComputeServiceError("compute_node_invalid")
    credential_ref = (credential_raw or "").strip() or None
    if credential_ref and _CREDENTIAL_REF.fullmatch(credential_ref) is None:
        raise ComputeServiceError("compute_node_invalid")
    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise ComputeServiceError("compute_node_invalid")
    timeout = value.get("timeout_seconds", 8.0)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 1 <= float(timeout) <= 30
    ):
        raise ComputeServiceError("compute_node_invalid")
    return ComputeNode(
        id=node_id,
        label=label,
        location=location,
        base_url=base_url,
        manifest_path=manifest_path,
        health_path=health_path,
        credential_ref=credential_ref,
        enabled=enabled,
        timeout_seconds=float(timeout),
    )


@dataclass(frozen=True)
class ComputeHealth:
    status: str
    path: str | None = None


@dataclass(frozen=True)
class ComputeCapacity:
    max_concurrency: int | None = None
    in_flight: int | None = None
    busy: bool | None = None

    @property
    def saturated(self) -> bool:
        if self.busy is True:
            return True
        return bool(
            self.max_concurrency is not None
            and self.in_flight is not None
            and self.in_flight >= self.max_concurrency
        )


@dataclass(frozen=True)
class ComputeImageLimits:
    max_count: int | None = None
    max_bytes: int | None = None
    max_total_bytes: int | None = None
    formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComputeAudioFormats:
    input_formats: tuple[str, ...] = ()
    output_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComputeInterface:
    id: str
    protocol: str
    method: str
    path: str
    streaming: tuple[str, ...]

    def render_url(self, node: ComputeNode, **parameters: Any) -> str:
        placeholders = tuple(_PLACEHOLDER.findall(self.path))
        if set(parameters) != set(placeholders):
            raise ComputeServiceError("compute_interface_parameters_invalid")
        rendered = self.path
        for name in placeholders:
            value = _plain_text(
                parameters[name],
                maximum=200,
                code="compute_interface_parameters_invalid",
            )
            rendered = rendered.replace("{" + name + "}", quote(value, safe=""))
        rendered = _validate_path(
            rendered,
            code="compute_interface_invalid",
            allow_templates=False,
        )
        return node.url(rendered)


@dataclass(frozen=True)
class ComputeOffering:
    service_id: str
    kind: str
    model_id: str | None
    selected: bool
    enabled: bool
    status: str
    input_modalities: frozenset[str]
    output_modalities: frozenset[str]
    interfaces: tuple[ComputeInterface, ...]
    image: ComputeImageLimits
    audio: ComputeAudioFormats
    tools: bool | None
    thinking: bool | None
    context_length: int | None
    capacity: ComputeCapacity

    @property
    def busy(self) -> bool:
        return self.status == "busy" or self.capacity.saturated

    def interface(self, protocol: str) -> ComputeInterface:
        normalized = str(protocol or "").strip().casefold()
        for item in self.interfaces:
            if item.protocol == normalized:
                return item
        raise ComputeServiceError("compute_interface_not_found")

    def interface_url(
        self,
        node: ComputeNode,
        protocol: str,
        **parameters: Any,
    ) -> str:
        return self.interface(protocol).render_url(node, **parameters)


@dataclass(frozen=True)
class ComputeReference:
    node_id: str
    service_id: str
    model_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "node_id",
            _identifier(self.node_id, code="compute_reference_invalid"),
        )
        object.__setattr__(
            self,
            "service_id",
            _identifier(self.service_id, code="compute_reference_invalid"),
        )
        if self.model_id is not None:
            object.__setattr__(
                self,
                "model_id",
                _plain_text(
                    self.model_id,
                    maximum=200,
                    code="compute_reference_invalid",
                ),
            )


@dataclass(frozen=True)
class ComputeRequirements:
    input_modalities: frozenset[str] = frozenset()
    output_modalities: frozenset[str] = frozenset()
    protocols: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()
    locations: frozenset[str] = frozenset()
    min_context_length: int | None = None

    def __post_init__(self) -> None:
        def identifiers(values: Any, *, protocol: bool = False) -> frozenset[str]:
            if isinstance(values, str) or not isinstance(
                values, (set, frozenset, list, tuple)
            ):
                raise ComputeServiceError("compute_requirements_invalid")
            normalized: set[str] = set()
            pattern = _PROTOCOL if protocol else _IDENTIFIER
            for raw in values:
                if not isinstance(raw, str):
                    raise ComputeServiceError("compute_requirements_invalid")
                item = raw.strip().casefold()
                if pattern.fullmatch(item) is None:
                    raise ComputeServiceError("compute_requirements_invalid")
                normalized.add(item)
            return frozenset(normalized)

        object.__setattr__(self, "input_modalities", identifiers(self.input_modalities))
        object.__setattr__(
            self, "output_modalities", identifiers(self.output_modalities)
        )
        object.__setattr__(
            self, "protocols", identifiers(self.protocols, protocol=True)
        )
        object.__setattr__(self, "features", identifiers(self.features))
        locations = identifiers(self.locations)
        if not locations.issubset({"local", "lan", "internet"}):
            raise ComputeServiceError("compute_requirements_invalid")
        object.__setattr__(self, "locations", locations)
        if self.min_context_length is not None and (
            type(self.min_context_length) is not int or self.min_context_length <= 0
        ):
            raise ComputeServiceError("compute_requirements_invalid")

    def matches(
        self, snapshot: "ComputeNodeSnapshot", offering: ComputeOffering
    ) -> bool:
        if self.locations and snapshot.privacy_location not in self.locations:
            return False
        if not self.input_modalities.issubset(offering.input_modalities):
            return False
        if not self.output_modalities.issubset(offering.output_modalities):
            return False
        available_protocols = frozenset(item.protocol for item in offering.interfaces)
        if not self.protocols.issubset(available_protocols):
            return False
        feature_values = {
            "tools": offering.tools is True,
            "thinking": offering.thinking is True,
        }
        if any(not feature_values.get(name, False) for name in self.features):
            return False
        if self.min_context_length is not None and (
            offering.context_length is None
            or offering.context_length < self.min_context_length
        ):
            return False
        return True


@dataclass(frozen=True)
class ResolvedComputeService:
    snapshot: "ComputeNodeSnapshot"
    offering: ComputeOffering

    @property
    def reference(self) -> ComputeReference:
        return ComputeReference(
            self.snapshot.node.id,
            self.offering.service_id,
            self.offering.model_id,
        )

    def interface_url(self, protocol: str, **parameters: Any) -> str:
        return self.offering.interface_url(
            self.snapshot.node,
            protocol,
            **parameters,
        )


@dataclass(frozen=True)
class ComputeNodeSnapshot:
    node: ComputeNode
    schema_version: int
    source_format: str
    name: str
    service_version: str
    config_revision: str
    privacy_location: str
    health: ComputeHealth
    capacity: ComputeCapacity
    offerings: tuple[ComputeOffering, ...]
    reachable: bool = True
    error_code: str | None = None

    def offering(self, service_id: str, model_id: str | None) -> ComputeOffering:
        service = _identifier(service_id, code="compute_reference_invalid")
        for offering in self.offerings:
            if offering.service_id == service and offering.model_id == model_id:
                return offering
        raise ComputeServiceError("offering_not_found")

    def catalogue(self) -> "ComputeCatalogue":
        return ComputeCatalogue((self,))


@dataclass(frozen=True)
class ComputeCatalogue:
    snapshots: tuple[ComputeNodeSnapshot, ...] = ()

    def _snapshot(self, node_id: str) -> ComputeNodeSnapshot:
        selected = _identifier(node_id, code="compute_reference_invalid")
        for snapshot in self.snapshots:
            if snapshot.node.id == selected:
                return snapshot
        raise ComputeServiceError("compute_node_not_discovered")

    def resolve(
        self,
        reference: ComputeReference,
        requirements: ComputeRequirements | None = None,
        *,
        allow_busy: bool = False,
    ) -> ResolvedComputeService:
        if not isinstance(reference, ComputeReference):
            raise ComputeServiceError("compute_reference_invalid")
        if requirements is not None and not isinstance(
            requirements, ComputeRequirements
        ):
            raise ComputeServiceError("compute_requirements_invalid")
        if type(allow_busy) is not bool:
            raise ComputeServiceError("compute_requirements_invalid")
        snapshot = self._snapshot(reference.node_id)
        if not snapshot.reachable:
            raise ComputeServiceError(snapshot.error_code or "compute_node_unreachable")
        if snapshot.health.status in _UNAVAILABLE_HEALTH or (
            snapshot.source_format == "formal"
            and snapshot.health.status not in _AVAILABLE_STATUSES
        ):
            raise ComputeServiceError("compute_node_unhealthy")
        if snapshot.capacity.saturated and not allow_busy:
            raise ComputeServiceError("compute_node_busy")
        offering = snapshot.offering(reference.service_id, reference.model_id)
        if not offering.enabled or offering.status not in _AVAILABLE_STATUSES | {
            "busy"
        }:
            raise ComputeServiceError("service_unavailable")
        if offering.busy and not allow_busy:
            raise ComputeServiceError("service_busy")
        if requirements is not None and not requirements.matches(snapshot, offering):
            raise ComputeServiceError("capability_mismatch")
        return ResolvedComputeService(snapshot, offering)

    def match(
        self,
        requirements: ComputeRequirements,
        *,
        allow_busy: bool = False,
    ) -> tuple[ResolvedComputeService, ...]:
        if (
            not isinstance(requirements, ComputeRequirements)
            or type(allow_busy) is not bool
        ):
            raise ComputeServiceError("compute_requirements_invalid")
        matches: list[ResolvedComputeService] = []
        for snapshot in self.snapshots:
            if (
                not snapshot.reachable
                or snapshot.health.status in _UNAVAILABLE_HEALTH
                or (
                    snapshot.source_format == "formal"
                    and snapshot.health.status not in _AVAILABLE_STATUSES
                )
                or (snapshot.capacity.saturated and not allow_busy)
            ):
                continue
            for offering in snapshot.offerings:
                if (
                    offering.enabled
                    and offering.status in _AVAILABLE_STATUSES | {"busy"}
                    and (allow_busy or not offering.busy)
                    and requirements.matches(snapshot, offering)
                ):
                    matches.append(ResolvedComputeService(snapshot, offering))
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    item.snapshot.node.id,
                    item.offering.service_id,
                    item.offering.model_id or "",
                ),
            )
        )


def _positive_int(value: Any, *, allow_none: bool, code: str) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int or value <= 0:
        raise ComputeServiceError(code)
    return value


def _nonnegative_int(value: Any, *, allow_none: bool, code: str) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int or value < 0:
        raise ComputeServiceError(code)
    return value


def _formats(value: Any, *, code: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ComputeServiceError(code)
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ComputeServiceError(code)
        item = raw.strip().casefold()
        if _FORMAT.fullmatch(item) is None:
            raise ComputeServiceError(code)
        if item not in result:
            result.append(item)
    return tuple(result)


def _modalities(value: Any, *, code: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ComputeServiceError(code)
    values: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ComputeServiceError(code)
        item = raw.strip().casefold()
        if _IDENTIFIER.fullmatch(item) is None:
            raise ComputeServiceError(code)
        values.add(item)
    return frozenset(values)


def _capacity(value: Any, *, formal: bool, status: str = "") -> ComputeCapacity:
    code = "manifest_capacity_invalid"
    if value is None and not formal:
        return ComputeCapacity(busy=(status == "busy" if status else None))
    if not isinstance(value, Mapping):
        raise ComputeServiceError(code)
    required = {"max_concurrency", "in_flight", "busy"}
    if formal and not required.issubset(value):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    maximum = _positive_int(
        value.get("max_concurrency"), allow_none=not formal, code=code
    )
    in_flight = _nonnegative_int(
        value.get("in_flight"), allow_none=not formal, code=code
    )
    raw_busy = value.get("busy")
    if raw_busy is None and not formal:
        busy = status == "busy" if status else None
    elif type(raw_busy) is bool:
        busy = raw_busy
    else:
        raise ComputeServiceError(code)
    if maximum is not None and in_flight is not None and in_flight > maximum:
        raise ComputeServiceError(code)
    if maximum is not None and in_flight is not None and in_flight >= maximum:
        busy = True
    return ComputeCapacity(maximum, in_flight, busy)


def _legacy_capabilities(
    kind: str, model: Mapping[str, Any]
) -> tuple[
    frozenset[str],
    frozenset[str],
    ComputeImageLimits,
    ComputeAudioFormats,
    bool | None,
    bool | None,
    int | None,
]:
    defaults = {
        "stt": ({"audio"}, {"text"}),
        "tts": ({"text"}, {"audio"}),
        "advanced_tts": ({"text"}, {"audio"}),
        "vision": ({"text", "image"}, {"text"}),
        "ollama": ({"text"}, {"text"}),
        "chat": ({"text"}, {"text"}),
        "embedding": ({"text"}, {"embedding"}),
        "realtime": ({"text", "audio"}, {"text", "audio"}),
    }
    input_values, output_values = defaults.get(kind, (set(), set()))
    listed = model.get("capabilities")
    listed_values = listed if isinstance(listed, list) else []
    flags = {item.strip().casefold() for item in listed_values if isinstance(item, str)}
    if "vision" in flags or "image" in flags:
        input_values = set(input_values) | {"image"}
    if "audio" in flags and not input_values and not output_values:
        input_values, output_values = {"audio"}, {"audio"}
    details = model.get("details") if isinstance(model.get("details"), Mapping) else {}
    context = details.get("context_length")
    if context is not None and (type(context) is not int or context <= 0):
        context = None
    return (
        frozenset(input_values),
        frozenset(output_values),
        ComputeImageLimits(),
        ComputeAudioFormats(
            input_formats=(
                _formats(
                    details.get("input_audio_formats"),
                    code="manifest_capabilities_invalid",
                )
                if isinstance(details.get("input_audio_formats"), list)
                else ()
            ),
            output_formats=(
                _formats(
                    details.get("output_audio_formats"),
                    code="manifest_capabilities_invalid",
                )
                if isinstance(details.get("output_audio_formats"), list)
                else ()
            ),
        ),
        True if "tools" in flags else False if isinstance(listed, list) else None,
        True if "thinking" in flags else False if isinstance(listed, list) else None,
        context,
    )


def _formal_capabilities(
    value: Any,
) -> tuple[
    frozenset[str],
    frozenset[str],
    ComputeImageLimits,
    ComputeAudioFormats,
    bool,
    bool,
    int | None,
]:
    if not isinstance(value, Mapping):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    required = {
        "input_modalities",
        "output_modalities",
        "tools",
        "thinking",
        "context_length",
        "image",
        "audio",
    }
    if not required.issubset(value):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    inputs = _modalities(
        value.get("input_modalities"), code="manifest_capabilities_invalid"
    )
    outputs = _modalities(
        value.get("output_modalities"), code="manifest_capabilities_invalid"
    )
    if type(value.get("tools")) is not bool or type(value.get("thinking")) is not bool:
        raise ComputeServiceError("manifest_capabilities_invalid")
    context = value.get("context_length")
    if context is not None and (type(context) is not int or context <= 0):
        raise ComputeServiceError("manifest_capabilities_invalid")
    image = value.get("image")
    if not isinstance(image, Mapping) or not {
        "max_count",
        "max_bytes",
        "max_total_bytes",
        "formats",
    }.issubset(image):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    image_limits = ComputeImageLimits(
        max_count=_positive_int(
            image.get("max_count"),
            allow_none="image" not in inputs,
            code="manifest_capabilities_invalid",
        ),
        max_bytes=_positive_int(
            image.get("max_bytes"),
            allow_none="image" not in inputs,
            code="manifest_capabilities_invalid",
        ),
        max_total_bytes=_positive_int(
            image.get("max_total_bytes"),
            allow_none="image" not in inputs,
            code="manifest_capabilities_invalid",
        ),
        formats=_formats(image.get("formats"), code="manifest_capabilities_invalid"),
    )
    if "image" in inputs and not image_limits.formats:
        raise ComputeServiceError("manifest_capabilities_invalid")
    audio = value.get("audio")
    if not isinstance(audio, Mapping) or not {
        "input_formats",
        "output_formats",
    }.issubset(audio):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    audio_formats = ComputeAudioFormats(
        input_formats=_formats(
            audio.get("input_formats"), code="manifest_capabilities_invalid"
        ),
        output_formats=_formats(
            audio.get("output_formats"), code="manifest_capabilities_invalid"
        ),
    )
    if "audio" in inputs and not audio_formats.input_formats:
        raise ComputeServiceError("manifest_capabilities_invalid")
    if "audio" in outputs and not audio_formats.output_formats:
        raise ComputeServiceError("manifest_capabilities_invalid")
    return (
        inputs,
        outputs,
        image_limits,
        audio_formats,
        value["tools"],
        value["thinking"],
        context,
    )


_LEGACY_PROTOCOLS = {
    "/v1/chat/completions": "openai.chat.completions",
    "/v1/embeddings": "openai.embeddings",
    "/v1/audio/transcriptions": "openai.audio.transcriptions",
    "/v1/audio/speech": "openai.audio.speech",
    "/v1/audio/speech/expressive": "hashi.audio.speech.expressive",
    "/v1/voices": "hashi.voices.list",
    "/v1/voice-profiles": "hashi.voice_profiles.list",
    "/v1/capabilities": "hashi.capabilities",
    "/v1/expressive-capabilities": "hashi.expressive_capabilities",
    "/api/chat": "ollama.chat",
    "/api/generate": "ollama.generate",
    "/api/embed": "ollama.embed",
    "/api/embeddings": "ollama.embeddings",
}


def _legacy_protocol(path: str) -> str:
    if "sample" in path.casefold():
        return "hashi.voice.sample"
    return _LEGACY_PROTOCOLS.get(path, "http." + path.strip("/").replace("/", "."))


def _interfaces(value: Any, *, formal: bool) -> tuple[ComputeInterface, ...]:
    if not isinstance(value, list) or not value:
        if formal:
            raise ComputeServiceError("manifest_capabilities_incomplete")
        return ()
    result: list[ComputeInterface] = []
    seen_ids: set[str] = set()
    seen_protocols: set[str] = set()
    for index, raw in enumerate(value):
        if isinstance(raw, str) and not formal:
            path = _validate_path(
                raw,
                code="manifest_interface_invalid",
                allow_templates=True,
            )
            protocol = _legacy_protocol(path)
            method = (
                "GET"
                if protocol
                in {
                    "hashi.voices.list",
                    "hashi.voice_profiles.list",
                    "hashi.capabilities",
                    "hashi.expressive_capabilities",
                    "hashi.voice.sample",
                }
                else "POST"
            )
            item = ComputeInterface(
                id=f"legacy-{index + 1}",
                protocol=protocol,
                method=method,
                path=path,
                streaming=(),
            )
        elif isinstance(raw, Mapping):
            required = {"id", "protocol", "method", "path", "streaming"}
            if formal and not required.issubset(raw):
                raise ComputeServiceError("manifest_capabilities_incomplete")
            interface_id = _identifier(
                raw.get("id") or f"interface-{index + 1}",
                code="manifest_interface_invalid",
            )
            protocol_raw = raw.get("protocol")
            if not isinstance(protocol_raw, str):
                raise ComputeServiceError("manifest_interface_invalid")
            protocol = protocol_raw.strip().casefold()
            if _PROTOCOL.fullmatch(protocol) is None:
                raise ComputeServiceError("manifest_interface_invalid")
            method_raw = raw.get("method", "POST")
            if not isinstance(method_raw, str):
                raise ComputeServiceError("manifest_interface_invalid")
            method = method_raw.strip().upper()
            if method not in {"GET", "POST"}:
                raise ComputeServiceError("manifest_interface_invalid")
            path = _validate_path(
                raw.get("path"),
                code="manifest_interface_invalid",
                allow_templates=True,
            )
            stream_raw = raw.get("streaming", [])
            if not isinstance(stream_raw, list) or (formal and not stream_raw):
                raise ComputeServiceError("manifest_interface_invalid")
            streaming: list[str] = []
            for entry in stream_raw:
                if not isinstance(entry, str):
                    raise ComputeServiceError("manifest_interface_invalid")
                normalized = entry.strip().casefold()
                if _IDENTIFIER.fullmatch(normalized) is None:
                    raise ComputeServiceError("manifest_interface_invalid")
                if normalized not in streaming:
                    streaming.append(normalized)
            item = ComputeInterface(
                id=interface_id,
                protocol=protocol,
                method=method,
                path=path,
                streaming=tuple(streaming),
            )
        else:
            raise ComputeServiceError("manifest_interface_invalid")
        if item.id in seen_ids or item.protocol in seen_protocols:
            raise ComputeServiceError("manifest_interface_duplicate")
        seen_ids.add(item.id)
        seen_protocols.add(item.protocol)
        result.append(item)
    return tuple(result)


def _status(value: Any, *, default: str = "unknown") -> str:
    selected = default if value is None or value == "" else value
    if not isinstance(selected, str):
        raise ComputeServiceError("manifest_status_invalid")
    normalized = selected.strip().casefold()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ComputeServiceError("manifest_status_invalid")
    return normalized


def _hashi_compute_modalities(
    value: Any, *, allow_unknown: bool = False
) -> frozenset[str]:
    if not isinstance(value, Mapping):
        raise ComputeServiceError("manifest_capabilities_invalid")
    supported: set[str] = set()
    for raw_name, raw_status in value.items():
        name = _identifier(raw_name, code="manifest_capabilities_invalid")
        status = _status(raw_status)
        if status == "supported":
            supported.add(name)
        elif status == "unknown" and allow_unknown:
            continue
        elif status != "unsupported":
            raise ComputeServiceError("manifest_capabilities_invalid")
    return frozenset(supported)


def _hashi_compute_feature(
    value: Any, *, allow_unknown: bool = False
) -> bool | None:
    if not isinstance(value, Mapping):
        raise ComputeServiceError("manifest_capabilities_invalid")
    support = _status(value.get("support"))
    if support == "unknown" and allow_unknown:
        return None
    if support not in {"supported", "unsupported"}:
        raise ComputeServiceError("manifest_capabilities_invalid")
    return support == "supported"


def _hashi_compute_interfaces(value: Any) -> tuple[ComputeInterface, ...]:
    if not isinstance(value, list) or not value:
        raise ComputeServiceError("manifest_interface_invalid")
    interfaces: list[ComputeInterface] = []
    seen_protocols: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ComputeServiceError("manifest_interface_invalid")
        path = _validate_path(
            raw.get("path"),
            code="manifest_interface_invalid",
            allow_templates=True,
        )
        protocol = _legacy_protocol(path)
        if protocol in seen_protocols:
            raise ComputeServiceError("manifest_interface_duplicate")
        method_raw = raw.get("method")
        if not isinstance(method_raw, str):
            raise ComputeServiceError("manifest_interface_invalid")
        method = method_raw.strip().upper()
        if method not in {"GET", "POST"}:
            raise ComputeServiceError("manifest_interface_invalid")
        streaming_raw = raw.get("streaming")
        if not isinstance(streaming_raw, Mapping):
            raise ComputeServiceError("manifest_interface_invalid")
        support = _status(streaming_raw.get("support"))
        modes = streaming_raw.get("modes")
        if not isinstance(modes, list):
            raise ComputeServiceError("manifest_interface_invalid")
        streaming = tuple(
            _identifier(item, code="manifest_interface_invalid") for item in modes
        )
        if support == "unsupported":
            streaming = ("none",)
        elif support != "supported":
            raise ComputeServiceError("manifest_interface_invalid")
        interfaces.append(
            ComputeInterface(
                id=f"interface-{index + 1}",
                protocol=protocol,
                method=method,
                path=path,
                streaming=streaming,
            )
        )
        seen_protocols.add(protocol)
    return tuple(interfaces)


def _hashi_compute_capacity(value: Any) -> ComputeCapacity:
    if not isinstance(value, Mapping):
        return ComputeCapacity()
    maximum_raw = value.get("inference_parallelism")
    maximum = (
        maximum_raw
        if type(maximum_raw) is int and maximum_raw > 0
        else None
    )
    in_flight_raw = value.get("in_flight")
    in_flight = (
        in_flight_raw
        if type(in_flight_raw) is int and in_flight_raw >= 0
        else None
    )
    raw_busy = value.get("busy")
    raw_capacity = value.get("at_capacity")
    busy = (
        bool(raw_busy is True or raw_capacity is True)
        if type(raw_busy) is bool or type(raw_capacity) is bool
        else None
    )
    if maximum is not None and in_flight is not None and in_flight >= maximum:
        busy = True
    return ComputeCapacity(maximum, in_flight, busy)


def _parse_hashi_compute_manifest(
    node: ComputeNode,
    payload: Mapping[str, Any],
) -> ComputeNodeSnapshot:
    if payload.get("version") != HASHI_COMPUTE_MANIFEST_VERSION:
        raise ComputeServiceError("manifest_schema_unsupported")
    service = payload.get("service")
    privacy_raw = payload.get("privacy")
    if not isinstance(service, Mapping) or not isinstance(privacy_raw, Mapping):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    privacy = str(privacy_raw.get("client_access_location") or "").strip().casefold()
    if privacy != node.location:
        raise ComputeServiceError("manifest_location_mismatch")
    name = _plain_text(service.get("name"), maximum=100, code="manifest_invalid")
    version = _plain_text(
        service.get("version"),
        maximum=100,
        code="manifest_capabilities_incomplete",
    )
    revision_raw = service.get("configuration_revision")
    if isinstance(revision_raw, bool) or not isinstance(revision_raw, (str, int)):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    revision = str(revision_raw).strip()
    if not revision or len(revision) > 100:
        raise ComputeServiceError("manifest_invalid")

    health_raw = payload.get("health")
    if not isinstance(health_raw, Mapping):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    advertised_health_paths = {
        _validate_path(value, code="manifest_health_invalid")
        for value in health_raw.values()
        if isinstance(value, str) and value.strip()
    }
    if node.health_path and node.health_path not in advertised_health_paths:
        raise ComputeServiceError("manifest_health_mismatch")
    health_path = node.health_path or next(iter(sorted(advertised_health_paths)), None)

    gateway = payload.get("gateway")
    if not isinstance(gateway, Mapping):
        raise ComputeServiceError("manifest_capabilities_incomplete")
    maximum_raw = gateway.get("max_in_flight")
    in_flight_raw = gateway.get("in_flight")
    capacity = ComputeCapacity(
        max_concurrency=(
            maximum_raw
            if type(maximum_raw) is int and maximum_raw > 0
            else None
        ),
        in_flight=(
            in_flight_raw
            if type(in_flight_raw) is int and in_flight_raw >= 0
            else None
        ),
        busy=bool(
            gateway.get("busy") is True or gateway.get("at_capacity") is True
        ),
    )

    models = payload.get("models")
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_SERVICES * MAX_MODELS_PER_SERVICE:
        raise ComputeServiceError("manifest_services_invalid")
    offerings: list[ComputeOffering] = []
    for model in models:
        if not isinstance(model, Mapping):
            raise ComputeServiceError("manifest_model_invalid")
        model_id = _plain_text(
            model.get("id"), maximum=200, code="manifest_model_invalid"
        )
        service_id = _identifier(
            model.get("service_id"), code="manifest_service_invalid"
        )
        available = model.get("available")
        if type(available) is not bool:
            raise ComputeServiceError("manifest_model_invalid")
        runtime = model.get("runtime")
        if not isinstance(runtime, Mapping):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        readiness = _status(runtime.get("readiness"))
        model_capacity = _hashi_compute_capacity(runtime)
        status = (
            "unavailable"
            if not available
            else "busy"
            if model_capacity.saturated
            else readiness
        )
        modalities = model.get("modalities")
        if not isinstance(modalities, Mapping):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        inputs = _hashi_compute_modalities(
            modalities.get("input"), allow_unknown=not available
        )
        outputs = _hashi_compute_modalities(
            modalities.get("output"), allow_unknown=not available
        )
        if available and (not inputs or not outputs):
            raise ComputeServiceError("manifest_capabilities_invalid")

        image_raw = model.get("image")
        if not isinstance(image_raw, Mapping):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        image_support = _status(image_raw.get("support"))
        if image_support == "supported":
            image_limits = ComputeImageLimits(
                max_count=_positive_int(
                    image_raw.get("max_count"),
                    allow_none=False,
                    code="manifest_capabilities_invalid",
                ),
                max_bytes=_positive_int(
                    image_raw.get("max_bytes_each"),
                    allow_none=False,
                    code="manifest_capabilities_invalid",
                ),
                max_total_bytes=None,
                formats=_formats(
                    image_raw.get("formats"),
                    code="manifest_capabilities_invalid",
                ),
            )
        elif image_support == "unsupported" or (
            not available and image_support == "unknown"
        ):
            image_limits = ComputeImageLimits()
        else:
            raise ComputeServiceError("manifest_capabilities_invalid")

        audio_raw = model.get("audio")
        if not isinstance(audio_raw, Mapping):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        audio_formats = ComputeAudioFormats(
            input_formats=(
                _formats(
                    audio_raw.get("input_formats"),
                    code="manifest_capabilities_invalid",
                )
                if isinstance(audio_raw.get("input_formats"), list)
                else ()
            ),
            output_formats=(
                _formats(
                    audio_raw.get("output_formats"),
                    code="manifest_capabilities_invalid",
                )
                if isinstance(audio_raw.get("output_formats"), list)
                else ()
            ),
        )
        features = model.get("features")
        if not isinstance(features, Mapping):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        tools = _hashi_compute_feature(
            features.get("tools"), allow_unknown=not available
        )
        thinking = _hashi_compute_feature(
            features.get("thinking"), allow_unknown=not available
        )
        context_raw = model.get("context")
        if not isinstance(context_raw, Mapping):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        context_length = next(
            (
                value
                for value in (
                    context_raw.get("served_context_tokens"),
                    context_raw.get("model_max_tokens"),
                )
                if type(value) is int and value > 0
            ),
            None,
        )
        offerings.append(
            ComputeOffering(
                service_id=service_id,
                kind=service_id,
                model_id=model_id,
                selected=True,
                enabled=available,
                status=status,
                input_modalities=inputs,
                output_modalities=outputs,
                interfaces=_hashi_compute_interfaces(model.get("interfaces")),
                image=image_limits,
                audio=audio_formats,
                tools=tools,
                thinking=thinking,
                context_length=context_length,
                capacity=model_capacity,
            )
        )
    keys = [(item.service_id, item.model_id) for item in offerings]
    if len(keys) != len(set(keys)):
        raise ComputeServiceError("manifest_offering_duplicate")
    healthy = any(
        item.enabled and item.status in _AVAILABLE_STATUSES | {"busy"}
        for item in offerings
    )
    return ComputeNodeSnapshot(
        node=node,
        schema_version=HASHI_COMPUTE_MANIFEST_VERSION,
        source_format="hashi_compute_manifest",
        name=name,
        service_version=version,
        config_revision=revision,
        privacy_location=privacy,
        health=ComputeHealth("ready" if healthy else "unknown", health_path),
        capacity=capacity,
        offerings=tuple(offerings),
    )


def _parse_service(
    raw: Any,
    *,
    formal: bool,
    inherited_capacity: ComputeCapacity,
) -> tuple[ComputeOffering, ...]:
    if not isinstance(raw, Mapping):
        raise ComputeServiceError("manifest_service_invalid")
    service_id = _identifier(raw.get("id"), code="manifest_service_invalid")
    kind = _identifier(raw.get("kind"), code="manifest_service_invalid")
    enabled = raw.get("enabled", True)
    if type(enabled) is not bool:
        raise ComputeServiceError("manifest_service_invalid")
    service_status = _status(raw.get("status"), default="unknown")
    service_capacity = _capacity(
        raw.get("capacity"), formal=formal, status=service_status
    )
    service_interfaces_raw = raw.get("interfaces")
    if service_interfaces_raw is None:
        service_interfaces_raw = raw.get("endpoints")
    models = raw.get("models", [])
    if not isinstance(models, list) or len(models) > MAX_MODELS_PER_SERVICE:
        raise ComputeServiceError("manifest_service_invalid")
    if not models:
        if not service_interfaces_raw:
            return ()
        models = [{"id": None}]
    offerings: list[ComputeOffering] = []
    for model in models:
        if not isinstance(model, Mapping):
            raise ComputeServiceError("manifest_model_invalid")
        model_id_raw = model.get("id")
        model_id = (
            _plain_text(model_id_raw, maximum=200, code="manifest_model_invalid")
            if model_id_raw is not None
            else None
        )
        selected = model.get("selected", False)
        if type(selected) is not bool:
            raise ComputeServiceError("manifest_model_invalid")
        status = _status(model.get("status"), default=service_status)
        capacity = (
            _capacity(model.get("capacity"), formal=formal, status=status)
            if "capacity" in model
            else service_capacity
        )
        if capacity == ComputeCapacity() and inherited_capacity != ComputeCapacity():
            capacity = inherited_capacity
        interfaces_raw = model.get("interfaces")
        if interfaces_raw is None:
            interfaces_raw = model.get("endpoints")
        if interfaces_raw is None:
            interfaces_raw = service_interfaces_raw
        interfaces = _interfaces(interfaces_raw, formal=formal)
        if formal:
            capabilities_raw = model.get("capabilities", raw.get("capabilities"))
            capabilities = _formal_capabilities(capabilities_raw)
        else:
            capabilities_raw = model.get("capabilities", raw.get("capabilities"))
            capabilities = (
                _formal_capabilities(capabilities_raw)
                if isinstance(capabilities_raw, Mapping)
                else _legacy_capabilities(kind, model)
            )
        offerings.append(
            ComputeOffering(
                service_id=service_id,
                kind=kind,
                model_id=model_id,
                selected=selected,
                enabled=enabled,
                status=status,
                input_modalities=capabilities[0],
                output_modalities=capabilities[1],
                interfaces=interfaces,
                image=capabilities[2],
                audio=capabilities[3],
                tools=capabilities[4],
                thinking=capabilities[5],
                context_length=capabilities[6],
                capacity=capacity,
            )
        )
    return tuple(offerings)


def parse_compute_manifest(
    node: ComputeNode,
    payload: Any,
) -> ComputeNodeSnapshot:
    if not isinstance(node, ComputeNode) or not isinstance(payload, Mapping):
        raise ComputeServiceError("manifest_invalid")
    node.validated()
    declared_format = payload.get("format")
    if declared_format == HASHI_COMPUTE_MANIFEST_FORMAT:
        return _parse_hashi_compute_manifest(node, payload)
    if declared_format is not None:
        raise ComputeServiceError("manifest_schema_unsupported")
    declared_schema = payload.get("schema")
    formal = declared_schema is not None
    if formal and (
        declared_schema != COMPUTE_MANIFEST_SCHEMA
        or payload.get("schema_version") != COMPUTE_MANIFEST_VERSION
    ):
        raise ComputeServiceError("manifest_schema_unsupported")
    if formal:
        privacy = str(payload.get("privacy_location") or "").strip().casefold()
        if privacy != node.location:
            raise ComputeServiceError("manifest_location_mismatch")
        name = _plain_text(payload.get("name"), maximum=100, code="manifest_invalid")
        version = _plain_text(
            payload.get("version"), maximum=100, code="manifest_capabilities_incomplete"
        )
        revision_raw = payload.get("config_revision")
        if isinstance(revision_raw, bool) or not isinstance(revision_raw, (str, int)):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        if isinstance(revision_raw, int) and revision_raw < 0:
            raise ComputeServiceError("manifest_invalid")
        revision = str(revision_raw).strip()
        if not revision or len(revision) > 100:
            raise ComputeServiceError("manifest_invalid")
        health_raw = payload.get("health")
        if not isinstance(health_raw, Mapping) or not {"status", "path"}.issubset(
            health_raw
        ):
            raise ComputeServiceError("manifest_capabilities_incomplete")
        health_path = _validate_path(
            health_raw.get("path"), code="manifest_health_invalid"
        )
        if node.health_path and node.health_path != health_path:
            raise ComputeServiceError("manifest_health_mismatch")
        health = ComputeHealth(_status(health_raw.get("status")), health_path)
        capacity = _capacity(payload.get("capacity"), formal=True)
        schema_version = COMPUTE_MANIFEST_VERSION
        source_format = "formal"
    else:
        privacy = node.location
        name = str(payload.get("name") or node.label).strip()[:100] or node.label
        version = str(payload.get("version") or "unknown").strip()[:100] or "unknown"
        revision = (
            str(
                payload.get("config_revision", payload.get("revision", "unknown"))
            ).strip()[:100]
            or "unknown"
        )
        services_for_health = payload.get("services")
        statuses = []
        if isinstance(services_for_health, list):
            statuses = [
                _status(item.get("status"))
                for item in services_for_health
                if isinstance(item, Mapping) and item.get("enabled", True) is True
            ]
        derived_status = (
            "ready"
            if statuses and all(item in _AVAILABLE_STATUSES for item in statuses)
            else "unknown"
        )
        health = ComputeHealth(derived_status, node.health_path)
        capacity_raw = payload.get("capacity")
        if capacity_raw is None and isinstance(payload.get("metrics"), Mapping):
            metrics = payload["metrics"]
            capacity_raw = {
                key: metrics[key]
                for key in ("max_concurrency", "in_flight", "busy")
                if key in metrics
            }
        capacity = _capacity(capacity_raw, formal=False)
        schema_version = 0
        source_format = "legacy_overview"
    services = payload.get("services")
    if not isinstance(services, list) or not 1 <= len(services) <= MAX_SERVICES:
        raise ComputeServiceError("manifest_services_invalid")
    offerings: list[ComputeOffering] = []
    for raw in services:
        offerings.extend(
            _parse_service(raw, formal=formal, inherited_capacity=capacity)
        )
    if not offerings:
        raise ComputeServiceError("manifest_services_invalid")
    keys = [(item.service_id, item.model_id) for item in offerings]
    if len(keys) != len(set(keys)):
        raise ComputeServiceError("manifest_offering_duplicate")
    return ComputeNodeSnapshot(
        node=node,
        schema_version=schema_version,
        source_format=source_format,
        name=name,
        service_version=version,
        config_revision=revision,
        privacy_location=privacy,
        health=health,
        capacity=capacity,
        offerings=tuple(offerings),
    )


class ComputeServicesConfig:
    """Revisioned instance configuration for compute node trust boundaries."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        try:
            read_config_json(self.path)
        except FileNotFoundError:
            write_config_json(
                self.path,
                new_config_json(
                    self.path,
                    {"version": COMPUTE_CONFIG_VERSION, "nodes": []},
                ),
            )

    @staticmethod
    def _nodes(doc: Mapping[str, Any]) -> tuple[ComputeNode, ...]:
        if doc.get("version") != COMPUTE_CONFIG_VERSION:
            raise ComputeServiceError("compute_configuration_invalid")
        rows = doc.get("nodes")
        if not isinstance(rows, list) or len(rows) > MAX_NODES:
            raise ComputeServiceError("compute_configuration_invalid")
        nodes = tuple(validate_compute_node(row) for row in rows)
        ids = [node.id for node in nodes]
        if len(ids) != len(set(ids)):
            raise ComputeServiceError("compute_node_duplicate")
        return nodes

    def read(self) -> tuple[ConfigDocument, tuple[ComputeNode, ...]]:
        try:
            doc = read_config_json(self.path)
        except FileNotFoundError as exc:
            raise ComputeServiceError("compute_not_configured") from exc
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ComputeServiceError("compute_configuration_invalid") from exc
        return doc, self._nodes(doc)

    def write(self, doc: ConfigDocument) -> str:
        if not isinstance(doc, ConfigDocument):
            raise TypeError("compute configuration writes require a read revision")
        self._nodes(doc)
        return write_config_json(self.path, doc)


class ComputeServiceClient:
    """Bounded, no-redirect client for one configured compute node."""

    def __init__(
        self,
        *,
        session_factory=aiohttp.ClientSession,
        secret_resolver: Any = None,
    ) -> None:
        self.session_factory = session_factory
        self.secret_resolver = secret_resolver

    def _headers(self, node: ComputeNode) -> dict[str, str]:
        if not node.credential_ref:
            return {"Accept": "application/json"}
        if self.secret_resolver is None:
            raise ComputeServiceError("compute_credential_missing")
        try:
            value = self.secret_resolver.resolve(node.credential_ref).value
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ComputeServiceError("compute_credential_missing") from exc
        if not value:
            raise ComputeServiceError("compute_credential_missing")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {value}",
        }

    async def _get_json(self, node: ComputeNode, path: str) -> Mapping[str, Any]:
        headers = self._headers(node)
        try:
            async with self.session_factory(
                timeout=aiohttp.ClientTimeout(total=node.timeout_seconds),
                trust_env=False,
            ) as session:
                async with session.get(
                    node.url(path),
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ComputeServiceError("compute_manifest_rejected")
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[
                        0
                    ]
                    if media_type.casefold() not in {
                        "application/json",
                        "application/problem+json",
                    }:
                        raise ComputeServiceError(
                            "compute_manifest_content_type_invalid"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(16384):
                        total += len(chunk)
                        if total > MAX_MANIFEST_BYTES:
                            raise ComputeServiceError("compute_manifest_too_large")
                        chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ComputeServiceError("manifest_invalid") from exc
            if not isinstance(payload, Mapping):
                raise ComputeServiceError("manifest_invalid")
            return payload
        except asyncio.CancelledError:
            raise
        except ComputeServiceError:
            raise
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
        ) as exc:
            raise ComputeServiceError("compute_node_unreachable") from exc

    async def fetch_manifest(self, node: ComputeNode) -> ComputeNodeSnapshot:
        return parse_compute_manifest(
            node,
            await self._get_json(node, node.manifest_path),
        )

    async def invoke_json(
        self,
        resolved: ResolvedComputeService,
        protocol: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """POST JSON only to an interface resolved from the configured node origin."""

        if not isinstance(resolved, ResolvedComputeService) or not isinstance(
            payload, Mapping
        ):
            raise ComputeServiceError("compute_invocation_invalid")
        interface = resolved.offering.interface(protocol)
        if interface.method != "POST":
            raise ComputeServiceError("compute_interface_method_invalid")
        node = resolved.snapshot.node
        headers = {
            **self._headers(node),
            "Content-Type": "application/json",
        }
        try:
            async with self.session_factory(
                timeout=aiohttp.ClientTimeout(total=node.timeout_seconds),
                trust_env=False,
            ) as session:
                async with session.post(
                    resolved.interface_url(protocol),
                    headers=headers,
                    json=dict(payload),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ComputeServiceError("compute_invocation_rejected")
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[
                        0
                    ]
                    if media_type.casefold() not in {
                        "application/json",
                        "application/problem+json",
                    }:
                        raise ComputeServiceError(
                            "compute_invocation_content_type_invalid"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(16384):
                        total += len(chunk)
                        if total > MAX_INFERENCE_RESPONSE_BYTES:
                            raise ComputeServiceError(
                                "compute_invocation_response_too_large"
                            )
                        chunks.append(chunk)
            try:
                result = json.loads(b"".join(chunks).decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ComputeServiceError("compute_invocation_response_invalid") from exc
            if not isinstance(result, Mapping):
                raise ComputeServiceError("compute_invocation_response_invalid")
            return result
        except asyncio.CancelledError:
            raise
        except ComputeServiceError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise ComputeServiceError("compute_node_unreachable") from exc

    async def invoke_bytes(
        self,
        resolved: ResolvedComputeService,
        protocol: str,
        payload: Mapping[str, Any],
    ) -> tuple[bytes, str]:
        """POST JSON to one resolved interface and return bounded audio bytes."""

        if not isinstance(resolved, ResolvedComputeService) or not isinstance(
            payload, Mapping
        ):
            raise ComputeServiceError("compute_invocation_invalid")
        interface = resolved.offering.interface(protocol)
        if interface.method != "POST":
            raise ComputeServiceError("compute_interface_method_invalid")
        node = resolved.snapshot.node
        headers = {
            **self._headers(node),
            "Accept": "audio/*, application/octet-stream",
            "Content-Type": "application/json",
        }
        try:
            async with self.session_factory(
                timeout=aiohttp.ClientTimeout(total=node.timeout_seconds),
                trust_env=False,
            ) as session:
                async with session.post(
                    resolved.interface_url(protocol),
                    headers=headers,
                    json=dict(payload),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ComputeServiceError("compute_invocation_rejected")
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[
                        0
                    ].casefold()
                    if not (
                        media_type.startswith("audio/")
                        or media_type == "application/octet-stream"
                    ):
                        raise ComputeServiceError(
                            "compute_invocation_content_type_invalid"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(16384):
                        total += len(chunk)
                        if total > MAX_AUDIO_RESPONSE_BYTES:
                            raise ComputeServiceError(
                                "compute_invocation_response_too_large"
                            )
                        chunks.append(chunk)
            result = b"".join(chunks)
            if not result:
                raise ComputeServiceError("compute_invocation_response_invalid")
            return result, media_type
        except asyncio.CancelledError:
            raise
        except ComputeServiceError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise ComputeServiceError("compute_node_unreachable") from exc

    async def probe_health(
        self,
        node: ComputeNode,
        path: str | None = None,
    ) -> ComputeHealth:
        selected = path or node.health_path
        if not selected:
            raise ComputeServiceError("compute_health_not_configured")
        payload = await self._get_json(node, selected)
        return ComputeHealth(_status(payload.get("status")), selected)


@dataclass(frozen=True)
class ComputeRefreshResult:
    catalogue: ComputeCatalogue
    errors: Mapping[str, str] = field(default_factory=dict)
    config_revision: str | None = None


class ComputeServiceDirectory:
    """Atomically refresh configured nodes and expose immutable snapshots."""

    def __init__(
        self,
        config: ComputeServicesConfig,
        *,
        client: ComputeServiceClient | Any | None = None,
        max_parallel: int = 4,
    ) -> None:
        if type(max_parallel) is not int or not 1 <= max_parallel <= 16:
            raise ValueError("max_parallel must be between 1 and 16")
        self.config = config
        self.client = client or ComputeServiceClient()
        self.max_parallel = max_parallel
        self._catalogue = ComputeCatalogue()

    @property
    def catalogue(self) -> ComputeCatalogue:
        return self._catalogue

    async def refresh(self) -> ComputeRefreshResult:
        doc, configured = self.config.read()
        nodes = tuple(node for node in configured if node.enabled)
        previous = {
            snapshot.node.id: snapshot for snapshot in self._catalogue.snapshots
        }
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def fetch(node: ComputeNode):
            try:
                async with semaphore:
                    snapshot = await self.client.fetch_manifest(node)
                if (
                    not isinstance(snapshot, ComputeNodeSnapshot)
                    or snapshot.node != node
                ):
                    raise ComputeServiceError("manifest_invalid")
                return node.id, snapshot, None
            except asyncio.CancelledError:
                raise
            except ComputeServiceError as exc:
                return node.id, None, exc.code
            except Exception:
                return node.id, None, "compute_node_unreachable"

        rows = await asyncio.gather(*(fetch(node) for node in nodes))
        errors: dict[str, str] = {}
        snapshots: list[ComputeNodeSnapshot] = []
        for node_id, snapshot, error in rows:
            if snapshot is not None:
                snapshots.append(snapshot)
                continue
            errors[node_id] = str(error)
            old = previous.get(node_id)
            if old is not None:
                snapshots.append(replace(old, reachable=False, error_code=str(error)))
        self._catalogue = ComputeCatalogue(
            tuple(sorted(snapshots, key=lambda item: item.node.id))
        )
        return ComputeRefreshResult(
            catalogue=self._catalogue,
            errors=MappingProxyType(errors),
            config_revision=doc.revision,
        )


__all__ = [
    "COMPUTE_CONFIG_VERSION",
    "DEFAULT_COMPUTE_MANIFEST_PATH",
    "COMPUTE_MANIFEST_SCHEMA",
    "COMPUTE_MANIFEST_VERSION",
    "MAX_AUDIO_RESPONSE_BYTES",
    "MAX_INFERENCE_RESPONSE_BYTES",
    "HASHI_COMPUTE_MANIFEST_FORMAT",
    "HASHI_COMPUTE_MANIFEST_VERSION",
    "ComputeAudioFormats",
    "ComputeCapacity",
    "ComputeCatalogue",
    "ComputeHealth",
    "ComputeImageLimits",
    "ComputeInterface",
    "ComputeNode",
    "ComputeNodeSnapshot",
    "ComputeOffering",
    "ComputeReference",
    "ComputeRefreshResult",
    "ComputeRequirements",
    "ComputeServiceClient",
    "ComputeServiceDirectory",
    "ComputeServiceError",
    "ComputeServicesConfig",
    "ResolvedComputeService",
    "parse_compute_manifest",
    "validate_compute_node",
]
