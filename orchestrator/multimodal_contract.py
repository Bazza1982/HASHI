"""Provider-neutral request content, capability, and media routing contracts.

The objects in this module are safe to keep in request metadata: they contain
attachment identity and integrity information, never inline media bytes.  A
provider adapter may materialise bytes only for the final outbound request.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import mimetypes
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CANONICAL_REQUEST_CONTENT_VERSION = 1
HASHI_API_MAX_REQUEST_BYTES = 256 * 1024 * 1024
HASHI_API_MAX_IMAGE_BYTES = 50 * 1024 * 1024
INPUT_MODALITIES = frozenset({"text", "image", "audio", "video", "document"})
MEDIA_MODALITIES = INPUT_MODALITIES - {"text"}
CANONICAL_AUDIO_SEMANTIC_ROLES = frozenset(
    {"voice_message", "audio_attachment"}
)
INPUT_TRANSPORTS = frozenset(
    {"local_path", "data_url", "remote_url", "inline", "provider_file"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATA_URL_RE = re.compile(
    r"(?<![A-Za-z0-9_])data:[^,\s]*,",
    re.IGNORECASE,
)
_PERSISTENT_TRANSPORT_FIELDS = frozenset(
    {"update_id", "message_id", "media_group_id"}
)

# This is deliberately an exact, reviewed registry rather than a family-prefix
# guess.  A newly named model remains text-only until configuration or this
# registry explicitly proves its input shape.
_VERIFIED_NATIVE_IMAGE_MODELS: Mapping[str, frozenset[str]] = {
    "deepseek-api": frozenset(
        {
            "deepseek-v4-flash-vision-exp",
        }
    ),
    "openrouter-api": frozenset(
        {
            "google/gemini-2.5-pro",
            "google/gemini-2.5-flash",
            "google/gemini-2.5-flash-lite",
            "google/gemini-3.1-flash-lite-preview",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-opus-4.5",
        }
    ),
    "hashi-api": frozenset(
        {
            "gpt-5.6-luna",
            "gpt-5.6-sol",
        }
    ),
    "codex-cli": frozenset(
        {
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.3-codex-spark",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
        }
    ),
}


class MultimodalContractError(ValueError):
    """A stable, attachment-aware failure at the multimodal boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_MULTIMODAL_CONTENT",
        attachment_id: str = "",
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "INVALID_MULTIMODAL_CONTENT")
        self.attachment_id = str(attachment_id or "")


@dataclass(frozen=True)
class InputCapability:
    provider: str
    model: str
    input_modalities: frozenset[str] = frozenset({"text"})
    input_transports: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    limits: Mapping[str, int] = field(default_factory=dict)
    privacy_eligible: bool = True
    source: str = "unknown"

    def supports(self, modality: str, transport: str | None = None) -> bool:
        normalized = _normalise_modality(modality)
        if normalized not in self.input_modalities:
            return False
        if normalized == "text":
            return True
        if not self.privacy_eligible:
            return False
        if transport is None:
            return True
        return str(transport).strip().casefold() in self.input_transports.get(
            normalized, ()
        )


@dataclass(frozen=True)
class MediaRoutingDecision:
    attachment_id: str
    item_index: int
    modality: str
    route: str
    reason: str
    transport: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "item_index": self.item_index,
            "modality": self.modality,
            "route": self.route,
            "reason": self.reason,
            "transport": self.transport or None,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def _normalise_provider(value: Any) -> str:
    provider = str(value or "").strip().casefold().replace("_", "-")
    aliases = {
        "openrouter": "openrouter-api",
        "hashi": "hashi-api",
        "codex": "codex-cli",
    }
    return aliases.get(provider, provider)


def _normalise_modality(value: Any) -> str:
    modality = str(value or "").strip().casefold()
    aliases = {
        "photo": "image",
        "picture": "image",
        "voice": "audio",
        "pdf": "document",
        "file": "document",
    }
    return aliases.get(modality, modality)


def modality_for_attachment(
    kind: str,
    *,
    mime_type: str = "",
    filename: str = "",
) -> str:
    normalized_kind = str(kind or "").strip().casefold()
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().casefold()
    if normalized_kind in {"photo", "image", "sticker"}:
        return "image"
    if normalized_mime.startswith("image/"):
        return "image"
    if normalized_kind in {"audio", "voice"} or normalized_mime.startswith(
        "audio/"
    ):
        return "audio"
    if normalized_kind == "video" or normalized_mime.startswith("video/"):
        return "video"
    if normalized_kind == "voice_transcript":
        return "text"
    if normalized_kind == "document" or filename:
        return "document"
    return "document"


def infer_mime_type(filename: str, *, modality: str = "") -> str:
    guessed, _encoding = mimetypes.guess_type(str(filename or ""))
    if guessed:
        return guessed.casefold()
    defaults = {
        "image": "application/octet-stream",
        "audio": "application/octet-stream",
        "video": "application/octet-stream",
        "document": "application/octet-stream",
    }
    return defaults.get(_normalise_modality(modality), "application/octet-stream")


def _contains_forbidden_inline(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, str):
        return bool(_DATA_URL_RE.search(value))
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden_inline(key) or _contains_forbidden_inline(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_forbidden_inline(item) for item in value)
    return False


def contains_persistent_inline_media(value: Any) -> bool:
    """Detect bytes or data URLs that must not enter durable request state."""

    return _contains_forbidden_inline(value)


def _positive_item_index(value: Any) -> int:
    if isinstance(value, bool):
        raise MultimodalContractError("item_index must be a positive integer")
    try:
        index = int(value)
    except (TypeError, ValueError) as exc:
        raise MultimodalContractError(
            "item_index must be a positive integer"
        ) from exc
    if index <= 0:
        raise MultimodalContractError("item_index must be a positive integer")
    return index


def _normalise_transport(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MultimodalContractError("media transport must be an object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key)
        if normalized_key not in _PERSISTENT_TRANSPORT_FIELDS:
            raise MultimodalContractError(
                f"unsupported persistent media transport field {normalized_key!r}"
            )
        if item is None or item == "":
            continue
        if not isinstance(item, (str, int, float, bool)):
            raise MultimodalContractError(
                "media transport values must be JSON scalar values"
            )
        if isinstance(item, str) and len(item) > 512:
            raise MultimodalContractError(
                "media transport string values must not exceed 512 characters"
            )
        normalized[normalized_key] = item
    return normalized


def canonical_request_content(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate and return one persistent-safe canonical content envelope."""

    if isinstance(parts, (str, bytes, bytearray)) or not isinstance(parts, Sequence):
        raise MultimodalContractError("canonical request parts must be an array")
    if _contains_forbidden_inline(parts):
        raise MultimodalContractError(
            "persistent request content cannot contain bytes or data URLs",
            code="INLINE_MEDIA_PERSISTENCE_FORBIDDEN",
        )

    normalized_parts: list[dict[str, Any]] = []
    item_indexes: set[int] = set()
    attachment_ids: set[str] = set()
    local_refs: set[str] = set()
    for raw in parts:
        if not isinstance(raw, Mapping):
            raise MultimodalContractError("every canonical part must be an object")
        part_type = str(raw.get("type") or "").strip().casefold()
        item_index = _positive_item_index(raw.get("item_index"))
        if item_index in item_indexes:
            raise MultimodalContractError(
                f"duplicate canonical item_index {item_index}"
            )
        item_indexes.add(item_index)

        if part_type == "text":
            text = raw.get("text")
            if not isinstance(text, str):
                raise MultimodalContractError("text parts require a string text field")
            normalized_parts.append(
                {"type": "text", "item_index": item_index, "text": text}
            )
            continue
        if part_type != "media":
            raise MultimodalContractError(
                f"unsupported canonical part type {part_type!r}"
            )

        attachment_id = str(raw.get("attachment_id") or "").strip()
        if not attachment_id or attachment_id in attachment_ids:
            raise MultimodalContractError(
                "media parts require unique, non-empty attachment_id values",
                attachment_id=attachment_id,
            )
        attachment_ids.add(attachment_id)
        modality = _normalise_modality(raw.get("modality"))
        if modality not in MEDIA_MODALITIES:
            raise MultimodalContractError(
                f"unsupported media modality {modality!r}",
                attachment_id=attachment_id,
            )
        local_ref = raw.get("local_ref")
        if not isinstance(local_ref, str) or not local_ref.strip():
            raise MultimodalContractError(
                "media parts require a persistent local_ref",
                attachment_id=attachment_id,
            )
        if _DATA_URL_RE.search(local_ref):
            raise MultimodalContractError(
                "local_ref cannot contain an inline data URL",
                code="INLINE_MEDIA_PERSISTENCE_FORBIDDEN",
                attachment_id=attachment_id,
            )
        normalized_local_ref = local_ref.strip()
        if normalized_local_ref in local_refs:
            raise MultimodalContractError(
                "media parts require unique local_ref values",
                code="DUPLICATE_MEDIA_REFERENCE",
                attachment_id=attachment_id,
            )
        local_refs.add(normalized_local_ref)
        try:
            size_bytes = int(raw.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise MultimodalContractError(
                "media size_bytes must be a non-negative integer",
                attachment_id=attachment_id,
            ) from exc
        if size_bytes < 0:
            raise MultimodalContractError(
                "media size_bytes must be a non-negative integer",
                attachment_id=attachment_id,
            )
        sha256 = str(raw.get("sha256") or "").strip().casefold()
        if not _SHA256_RE.fullmatch(sha256):
            raise MultimodalContractError(
                "media sha256 must be a 64-character hexadecimal digest",
                attachment_id=attachment_id,
            )
        mime_type = str(raw.get("mime_type") or "").split(";", 1)[0].strip().casefold()
        if not mime_type or "/" not in mime_type:
            raise MultimodalContractError(
                "media mime_type is required",
                attachment_id=attachment_id,
            )
        expected_prefix = {
            "image": "image/",
            "audio": "audio/",
            "video": "video/",
        }.get(modality)
        if expected_prefix and not mime_type.startswith(expected_prefix):
            raise MultimodalContractError(
                f"media modality {modality!r} conflicts with MIME {mime_type!r}",
                code="MEDIA_SIGNATURE_MISMATCH",
                attachment_id=attachment_id,
            )
        detail = str(raw.get("detail") or "").strip().casefold()
        if detail and detail not in {"auto", "low", "high", "original"}:
            raise MultimodalContractError(
                "image detail must be auto, low, high, or original",
                attachment_id=attachment_id,
            )
        part = {
            "type": "media",
            "item_index": item_index,
            "attachment_id": attachment_id,
            "modality": modality,
            "kind": str(raw.get("kind") or modality).strip().casefold(),
            "mime_type": mime_type,
            "filename": str(raw.get("filename") or ""),
            "caption": str(raw.get("caption") or ""),
            "local_ref": normalized_local_ref,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "transport": _normalise_transport(raw.get("transport")),
        }
        if modality == "audio":
            semantic_role = str(
                raw.get("semantic_role") or "audio_attachment"
            ).strip().casefold()
            if semantic_role not in CANONICAL_AUDIO_SEMANTIC_ROLES:
                raise MultimodalContractError(
                    "audio semantic_role must be voice_message or audio_attachment",
                    code="INVALID_AUDIO_SEMANTIC_ROLE",
                    attachment_id=attachment_id,
                )
            part["semantic_role"] = semantic_role
            if raw.get("duration_ms") is not None:
                duration_ms = raw.get("duration_ms")
                if isinstance(duration_ms, bool):
                    raise MultimodalContractError(
                        "audio duration_ms must be a non-negative integer",
                        attachment_id=attachment_id,
                    )
                try:
                    duration_ms = int(duration_ms)
                except (TypeError, ValueError) as exc:
                    raise MultimodalContractError(
                        "audio duration_ms must be a non-negative integer",
                        attachment_id=attachment_id,
                    ) from exc
                if duration_ms < 0:
                    raise MultimodalContractError(
                        "audio duration_ms must be a non-negative integer",
                        attachment_id=attachment_id,
                    )
                part["duration_ms"] = duration_ms
        elif raw.get("semantic_role") is not None:
            raise MultimodalContractError(
                "semantic_role is only supported for audio media",
                code="INVALID_AUDIO_SEMANTIC_ROLE",
                attachment_id=attachment_id,
            )
        if detail:
            part["detail"] = detail
        normalized_parts.append(part)

    normalized_parts.sort(key=lambda item: item["item_index"])
    return {
        "version": CANONICAL_REQUEST_CONTENT_VERSION,
        "parts": normalized_parts,
    }


def normalize_request_content(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MultimodalContractError("canonical request content must be an object")
    try:
        version = int(value.get("version"))
    except (TypeError, ValueError) as exc:
        raise MultimodalContractError("canonical request version is invalid") from exc
    if version != CANONICAL_REQUEST_CONTENT_VERSION:
        raise MultimodalContractError(
            f"unsupported canonical request version {version}"
        )
    raw_parts = value.get("parts")
    if not isinstance(raw_parts, list):
        raise MultimodalContractError("canonical request parts must be an array")
    return canonical_request_content(raw_parts)


def attachment_manifest(
    request_content: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    normalized = normalize_request_content(request_content)
    if normalized is None:
        return ()
    return tuple(
        copy.deepcopy(part)
        for part in normalized["parts"]
        if part["type"] == "media"
    )


def native_attachment_reference_aliases(
    manifest: Iterable[Mapping[str, Any]],
    attachment_ids: Iterable[str],
) -> set[str]:
    """Build unambiguous request-local aliases for duplicate-route guards."""

    entries = [dict(item) for item in manifest if isinstance(item, Mapping)]
    selected = {str(item) for item in attachment_ids if str(item).strip()}
    basename_counts: dict[str, int] = {}
    for item in entries:
        local_ref = str(item.get("local_ref") or "").strip()
        if not local_ref:
            continue
        basename = Path(local_ref).name
        if basename:
            basename_counts[basename] = basename_counts.get(basename, 0) + 1

    aliases: set[str] = set()
    for item in entries:
        if str(item.get("attachment_id") or "") not in selected:
            continue
        local_ref = str(item.get("local_ref") or "").strip()
        if not local_ref:
            continue
        aliases.add(local_ref)
        try:
            aliases.add(str(Path(local_ref).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError, ValueError):
            pass
        basename = Path(local_ref).name
        if basename and basename_counts.get(basename) == 1:
            aliases.add(basename)
            aliases.add(f"./{basename}")
    return aliases


def request_content_has_media(request_content: Mapping[str, Any] | None) -> bool:
    return bool(attachment_manifest(request_content))


def request_content_is_voice_origin(
    request_content: Mapping[str, Any] | None,
) -> bool:
    """Return whether the user Turn explicitly originated as a voice message.

    Audio attachments used as files, sound effects, or evidence do not trigger
    a spoken response.  Only the persistent semantic role is authoritative.
    """

    return any(
        part.get("modality") == "audio"
        and part.get("semantic_role") == "voice_message"
        for part in attachment_manifest(request_content)
    )


def subset_request_content(
    request_content: Mapping[str, Any] | None,
    attachment_ids: Iterable[str],
    *,
    include_text: bool = True,
) -> dict[str, Any] | None:
    normalized = normalize_request_content(request_content)
    if normalized is None:
        return None
    selected = {str(item) for item in attachment_ids if str(item).strip()}
    parts = [
        part
        for part in normalized["parts"]
        if (include_text and part["type"] == "text")
        or (part["type"] == "media" and part["attachment_id"] in selected)
    ]
    return canonical_request_content(parts) if parts else None


def _tuple_transports(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        candidates = list(value)
    else:
        candidates = []
    normalized: list[str] = []
    for candidate in candidates:
        transport = str(candidate or "").strip().casefold()
        if transport in INPUT_TRANSPORTS and transport not in normalized:
            normalized.append(transport)
    return tuple(normalized)


def _registry_capability(provider: str, model: str) -> InputCapability | None:
    normalized_model = str(model or "").strip().casefold()
    if normalized_model not in _VERIFIED_NATIVE_IMAGE_MODELS.get(
        provider, frozenset()
    ):
        return None
    image_transports = (
        ("local_path", "data_url", "remote_url")
        if provider == "codex-cli"
        else ("data_url", "remote_url")
    )
    limits = {
        "item_count": 20,
        "item_bytes": 20 * 1024 * 1024,
        "total_bytes": 50 * 1024 * 1024,
    }
    if provider == "deepseek-api":
        # DeepSeek accepts up to 600 images and 32 MiB per regular image.  Its
        # HTTP body limit is 48 MiB, so cap decoded aggregate bytes at 32 MiB;
        # base64 expansion then remains below the provider body boundary with
        # room for the surrounding JSON and text prompt.
        limits = {
            "item_count": 600,
            "item_bytes": 32 * 1024 * 1024,
            "total_bytes": 32 * 1024 * 1024,
        }
    if provider == "hashi-api":
        # HASHI's serialized 256 MiB request limit is the aggregate boundary.
        # Keeping no lower decoded-total cap lets a 50 MiB current image coexist
        # with conversation history and earlier screenshots in the same body.
        limits = {
            "item_count": 20,
            "item_bytes": HASHI_API_MAX_IMAGE_BYTES,
        }
    return InputCapability(
        provider=provider,
        model=model,
        input_modalities=frozenset({"text", "image"}),
        input_transports={"image": image_transports},
        limits=limits,
        source="registry",
    )


def _explicit_capability(
    provider: str,
    model: str,
    config: Mapping[str, Any],
) -> InputCapability | None:
    nested = config.get("multimodal_input")
    options = dict(config)
    if isinstance(nested, Mapping):
        options.update(dict(nested))
    has_explicit = any(
        key in options
        for key in (
            "input_modalities",
            "input_transports",
            "input_limits",
            "multimodal_limits",
            "image_input",
        )
    )
    image_input_mode = str(options.get("image_input") or "").strip().casefold()
    legacy_native = image_input_mode == "native"
    if not has_explicit and not legacy_native:
        return None

    modalities_declared = "input_modalities" in options
    raw_modalities = options.get("input_modalities")
    # An explicit declaration is exact.  In particular, ``["audio"]`` must
    # remain audio-only so the runtime can adapt a text Turn instead of
    # silently sending an unsupported text-only request.  Text remains the
    # fail-closed default only when no explicit modality list exists.
    modalities: set[str] = set() if modalities_declared else {"text"}
    if isinstance(raw_modalities, str):
        raw_modalities = [raw_modalities]
    if isinstance(raw_modalities, (list, tuple, set, frozenset)):
        for item in raw_modalities:
            modality = _normalise_modality(item)
            if modality in INPUT_MODALITIES:
                modalities.add(modality)
    elif legacy_native:
        modalities.add("image")
    if not modalities:
        raise MultimodalContractError(
            "input_modalities must declare at least one supported modality"
        )

    raw_transports = options.get("input_transports")
    transports: dict[str, tuple[str, ...]] = {}
    if isinstance(raw_transports, Mapping):
        for raw_modality, raw_values in raw_transports.items():
            modality = _normalise_modality(raw_modality)
            if modality in MEDIA_MODALITIES:
                values = _tuple_transports(raw_values)
                if values:
                    transports[modality] = values
    if "image" in modalities and "image" not in transports:
        transports["image"] = (
            ("local_path",)
            if provider == "codex-cli"
            else ("data_url",)
        )

    raw_limits = options.get("input_limits")
    if not isinstance(raw_limits, Mapping):
        raw_limits = options.get("multimodal_limits")
    limits: dict[str, int] = {}
    if isinstance(raw_limits, Mapping):
        for key in (
            "item_count",
            "item_bytes",
            "total_bytes",
            "dimensions",
            "duration",
            "page_count",
        ):
            if key not in raw_limits:
                continue
            try:
                parsed = int(raw_limits[key])
            except (TypeError, ValueError) as exc:
                raise MultimodalContractError(
                    f"multimodal limit {key!r} must be a positive integer"
                ) from exc
            if parsed <= 0:
                raise MultimodalContractError(
                    f"multimodal limit {key!r} must be a positive integer"
                )
            limits[key] = parsed

    privacy_eligible = options.get("native_media_allowed", True)
    if not isinstance(privacy_eligible, bool):
        raise MultimodalContractError(
            "native_media_allowed must be a boolean"
        )

    return InputCapability(
        provider=provider,
        model=model,
        input_modalities=frozenset(modalities),
        input_transports=transports,
        limits=limits,
        privacy_eligible=privacy_eligible,
        source="explicit_config",
    )


def _privacy_eligibility_override(
    config: Mapping[str, Any] | None,
) -> bool | None:
    if not config:
        return None
    options = dict(config)
    nested = config.get("multimodal_input")
    if isinstance(nested, Mapping):
        options.update(dict(nested))
    if "native_media_allowed" not in options:
        return None
    value = options["native_media_allowed"]
    if not isinstance(value, bool):
        raise MultimodalContractError("native_media_allowed must be a boolean")
    return value


def _apply_privacy_override(
    capability: InputCapability,
    override: bool | None,
) -> InputCapability:
    if override is None or override == capability.privacy_eligible:
        return capability
    return InputCapability(
        provider=capability.provider,
        model=capability.model,
        input_modalities=capability.input_modalities,
        input_transports=capability.input_transports,
        limits=capability.limits,
        privacy_eligible=override,
        source=f"{capability.source}+privacy_config",
    )


def resolve_input_capability(
    provider: str,
    model: str,
    *,
    config: Mapping[str, Any] | None = None,
    verified_registry: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> InputCapability:
    """Resolve exact provider/model input support, failing closed by default."""

    normalized_provider = _normalise_provider(provider)
    normalized_model = str(model or "").strip()
    privacy_override = _privacy_eligibility_override(config)
    registry_key = (normalized_provider, normalized_model.casefold())
    if verified_registry and registry_key in verified_registry:
        registry_options = dict(verified_registry[registry_key])
        registry_options.setdefault("native_media_allowed", True)
        capability = _explicit_capability(
            normalized_provider, normalized_model, registry_options
        )
        if capability is not None:
            return _apply_privacy_override(
                InputCapability(
                    provider=capability.provider,
                    model=capability.model,
                    input_modalities=capability.input_modalities,
                    input_transports=capability.input_transports,
                    limits=capability.limits,
                    privacy_eligible=capability.privacy_eligible,
                    source="verified_registry",
                ),
                privacy_override,
            )
    if config:
        explicit = _explicit_capability(
            normalized_provider, normalized_model, config
        )
        if explicit is not None:
            return _apply_privacy_override(explicit, privacy_override)
    registered = _registry_capability(normalized_provider, normalized_model)
    if registered is not None:
        return _apply_privacy_override(registered, privacy_override)
    return _apply_privacy_override(
        InputCapability(
            provider=normalized_provider,
            model=normalized_model,
            input_modalities=frozenset({"text"}),
            input_transports={},
            limits={},
            privacy_eligible=True,
            source="unknown_fail_closed",
        ),
        privacy_override,
    )


def _preferred_transport(
    capability: InputCapability,
    modality: str,
    *,
    transport_preferences: Mapping[str, Sequence[str]] | None = None,
) -> str:
    available = capability.input_transports.get(modality, ())
    if transport_preferences is not None and modality in transport_preferences:
        preferred_values = tuple(transport_preferences[modality])
    else:
        preferred_values = (
            "data_url",
            "remote_url",
            "provider_file",
            "inline",
            "local_path",
        )
    for preferred in preferred_values:
        preferred = str(preferred or "").strip().casefold()
        if preferred in available:
            return preferred
    return ""


def route_request_content(
    request_content: Mapping[str, Any] | None,
    capability: InputCapability,
    *,
    fallback_modalities: Iterable[str] = (),
    transport_preferences: Mapping[str, Sequence[str]] | None = None,
) -> tuple[MediaRoutingDecision, ...]:
    normalized = normalize_request_content(request_content)
    if normalized is None:
        return ()
    media_parts = [part for part in normalized["parts"] if part["type"] == "media"]
    fallback = {_normalise_modality(item) for item in fallback_modalities}
    item_count_limit = int(capability.limits.get("item_count") or 0)
    item_bytes_limit = int(capability.limits.get("item_bytes") or 0)
    total_bytes_limit = int(capability.limits.get("total_bytes") or 0)
    decisions: list[MediaRoutingDecision] = []
    native_item_count = 0
    native_total_bytes = 0

    for part in media_parts:
        attachment_id = part["attachment_id"]
        modality = part["modality"]
        native_reason = "native_capability_available"
        native = capability.supports(modality)
        transport = ""
        if not capability.privacy_eligible:
            native = False
            native_reason = "privacy_policy_requires_local"
        elif not native:
            native_reason = "native_capability_unavailable"
        else:
            transport = _preferred_transport(
                capability,
                modality,
                transport_preferences=transport_preferences,
            )
        if native and not transport:
            native = False
            native_reason = "native_transport_unavailable"
        elif native and modality == "image" and capability.limits.get("dimensions"):
            native = False
            native_reason = "native_dimensions_limit_exceeded_unverified"
        elif (
            native
            and modality in {"audio", "video"}
            and capability.limits.get("duration")
        ):
            duration_ms = part.get("duration_ms")
            maximum_ms = int(capability.limits["duration"]) * 1000
            if duration_ms is None:
                native = False
                native_reason = "native_duration_limit_unverified"
            elif int(duration_ms) > maximum_ms:
                native = False
                native_reason = "native_duration_limit_exceeded"
        elif (
            native
            and modality == "document"
            and capability.limits.get("page_count")
        ):
            native = False
            native_reason = "native_page_count_limit_exceeded_unverified"
        elif native and item_count_limit and native_item_count >= item_count_limit:
            native = False
            native_reason = "native_item_count_limit_exceeded"
        elif (
            native
            and item_bytes_limit
            and int(part["size_bytes"]) > item_bytes_limit
        ):
            native = False
            native_reason = "native_item_size_limit_exceeded"
        elif (
            native
            and total_bytes_limit
            and native_total_bytes + int(part["size_bytes"]) > total_bytes_limit
        ):
            native = False
            native_reason = "native_total_size_limit_exceeded"

        if native:
            native_item_count += 1
            native_total_bytes += int(part["size_bytes"])
            decisions.append(
                MediaRoutingDecision(
                    attachment_id,
                    int(part["item_index"]),
                    modality,
                    "native",
                    native_reason,
                    transport,
                )
            )
            continue
        if modality in fallback:
            decisions.append(
                MediaRoutingDecision(
                    attachment_id,
                    int(part["item_index"]),
                    modality,
                    "local_fallback",
                    native_reason,
                )
            )
            continue
        decisions.append(
            MediaRoutingDecision(
                attachment_id,
                int(part["item_index"]),
                modality,
                "unsupported",
                native_reason,
            )
        )
    return tuple(decisions)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_signature(payload: bytes, mime_type: str, attachment_id: str) -> None:
    checks = {
        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
        "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": payload.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": payload.startswith(b"RIFF") and payload[8:12] == b"WEBP",
        "image/bmp": payload.startswith(b"BM"),
        "image/tiff": payload.startswith((b"II*\x00", b"MM\x00*")),
        "audio/ogg": payload.startswith(b"OggS"),
        "audio/opus": payload.startswith(b"OggS"),
        "audio/wav": payload.startswith(b"RIFF") and payload[8:12] == b"WAVE",
        "audio/x-wav": payload.startswith(b"RIFF") and payload[8:12] == b"WAVE",
        "audio/flac": payload.startswith(b"fLaC"),
        "audio/mpeg": payload.startswith(b"ID3")
        or (
            len(payload) >= 2
            and payload[0] == 0xFF
            and (payload[1] & 0xE0) == 0xE0
        ),
        "audio/aac": len(payload) >= 2
        and payload[0] == 0xFF
        and (payload[1] & 0xF6) == 0xF0,
        "audio/mp4": len(payload) >= 12 and payload[4:8] == b"ftyp",
        "audio/webm": payload.startswith(b"\x1aE\xdf\xa3"),
        "video/mp4": len(payload) >= 12 and payload[4:8] == b"ftyp",
        "video/quicktime": len(payload) >= 12 and payload[4:8] == b"ftyp",
        "video/webm": payload.startswith(b"\x1aE\xdf\xa3"),
        "video/x-matroska": payload.startswith(b"\x1aE\xdf\xa3"),
        "video/x-msvideo": payload.startswith(b"RIFF")
        and payload[8:12] == b"AVI ",
        "video/ogg": payload.startswith(b"OggS"),
        "application/pdf": payload.startswith(b"%PDF-"),
    }
    valid = checks.get(mime_type)
    if valid is None:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} uses an unverified MIME signature {mime_type}",
            code="MEDIA_SIGNATURE_UNVERIFIED",
            attachment_id=attachment_id,
        )
    if valid is False:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} does not match declared MIME {mime_type}",
            code="MEDIA_SIGNATURE_MISMATCH",
            attachment_id=attachment_id,
        )


def validate_inline_image_data_url(
    value: str,
    *,
    max_bytes: int = 0,
) -> tuple[str, int]:
    """Validate an ephemeral OpenAI-style image data URL without retaining it."""

    raw = str(value or "").strip()
    header, separator, encoded = raw.partition(",")
    if not raw.casefold().startswith("data:") or not separator:
        raise MultimodalContractError("inline image must be a data URL")
    if ";base64" not in header.casefold():
        raise MultimodalContractError("inline image data URL must use base64")
    mime_type = header[5:].split(";", 1)[0].strip().casefold()
    if not mime_type.startswith("image/"):
        raise MultimodalContractError(
            f"inline image declares unsupported MIME {mime_type!r}",
            code="MEDIA_SIGNATURE_MISMATCH",
        )
    padding = len(encoded) - len(encoded.rstrip("="))
    estimated_bytes = max(0, (len(encoded) * 3) // 4 - padding)
    if max_bytes and estimated_bytes > int(max_bytes):
        raise MultimodalContractError(
            "inline image exceeds the configured byte limit",
            code="MEDIA_LIMIT_EXCEEDED",
        )
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MultimodalContractError("inline image contains invalid base64") from exc
    if max_bytes and len(payload) > int(max_bytes):
        raise MultimodalContractError(
            "inline image exceeds the configured byte limit",
            code="MEDIA_LIMIT_EXCEEDED",
        )
    _validate_signature(payload, mime_type, "gateway-inline-image")
    return mime_type, len(payload)


def _read_authorized_media(
    part: Mapping[str, Any],
    *,
    authorized_roots: Sequence[str | Path],
    validate_signature: bool = True,
    return_payload: bool = True,
) -> bytes:
    attachment_id = str(part["attachment_id"])
    source = Path(str(part["local_ref"]))
    absolute_source = source.expanduser().absolute()
    current = absolute_source
    has_symlink_component = False
    while True:
        if current.is_symlink():
            has_symlink_component = True
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    if has_symlink_component:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} is a symlink",
            code="MEDIA_PATH_NOT_AUTHORIZED",
            attachment_id=attachment_id,
        )
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} is unavailable",
            code="MEDIA_UNAVAILABLE",
            attachment_id=attachment_id,
        ) from exc
    roots: list[Path] = []
    for root in authorized_roots:
        try:
            roots.append(Path(root).resolve(strict=True))
        except OSError:
            continue
    if not roots or not any(_is_within(resolved, root) for root in roots):
        raise MultimodalContractError(
            f"attachment {attachment_id!r} is outside its authorized roots",
            code="MEDIA_PATH_NOT_AUTHORIZED",
            attachment_id=attachment_id,
        )
    declared_size = int(part["size_bytes"])
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} is unavailable",
            code="MEDIA_UNAVAILABLE",
            attachment_id=attachment_id,
        ) from exc

    digest_builder = hashlib.sha256()
    signature_prefix = bytearray()
    payload_chunks: list[bytes] | None = [] if return_payload else None
    observed_size = 0
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise MultimodalContractError(
                    f"attachment {attachment_id!r} is not a regular file",
                    code="MEDIA_UNAVAILABLE",
                    attachment_id=attachment_id,
                )
            if int(opened_stat.st_size) != declared_size:
                raise MultimodalContractError(
                    f"attachment {attachment_id!r} size changed after intake",
                    code="MEDIA_INTEGRITY_CHANGED",
                    attachment_id=attachment_id,
                )
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                observed_size += len(chunk)
                if observed_size > declared_size:
                    raise MultimodalContractError(
                        f"attachment {attachment_id!r} size changed after intake",
                        code="MEDIA_INTEGRITY_CHANGED",
                        attachment_id=attachment_id,
                    )
                digest_builder.update(chunk)
                if len(signature_prefix) < 64:
                    signature_prefix.extend(chunk[: 64 - len(signature_prefix)])
                if payload_chunks is not None:
                    payload_chunks.append(chunk)
    except MultimodalContractError:
        raise
    except OSError as exc:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} could not be read safely",
            code="MEDIA_UNAVAILABLE",
            attachment_id=attachment_id,
        ) from exc

    if observed_size != declared_size:
        raise MultimodalContractError(
            f"attachment {attachment_id!r} size changed after intake",
            code="MEDIA_INTEGRITY_CHANGED",
            attachment_id=attachment_id,
        )
    digest = digest_builder.hexdigest()
    if digest != str(part["sha256"]):
        raise MultimodalContractError(
            f"attachment {attachment_id!r} hash changed after intake",
            code="MEDIA_INTEGRITY_CHANGED",
            attachment_id=attachment_id,
        )
    if validate_signature:
        _validate_signature(
            bytes(signature_prefix),
            str(part["mime_type"]),
            attachment_id,
        )
    return b"" if payload_chunks is None else b"".join(payload_chunks)


def validate_authorized_media_references(
    request_content: Mapping[str, Any] | None,
    *,
    authorized_roots: Sequence[str | Path],
) -> dict[str, Path]:
    """Validate every local reference before native or fallback routing."""

    normalized = normalize_request_content(request_content)
    if normalized is None:
        return {}
    resolved: dict[str, Path] = {}
    resolved_paths: set[Path] = set()
    for part in normalized["parts"]:
        if part["type"] != "media":
            continue
        modality = str(part["modality"])
        mime_type = str(part["mime_type"])
        signature_required = modality in {"image", "audio", "video"} or (
            modality == "document" and mime_type == "application/pdf"
        )
        _read_authorized_media(
            part,
            authorized_roots=authorized_roots,
            validate_signature=signature_required,
            return_payload=False,
        )
        resolved_path = Path(str(part["local_ref"])).resolve(strict=True)
        if resolved_path in resolved_paths:
            raise MultimodalContractError(
                "multiple attachments resolve to the same local media file",
                code="DUPLICATE_MEDIA_REFERENCE",
                attachment_id=str(part["attachment_id"]),
            )
        resolved_paths.add(resolved_path)
        resolved[str(part["attachment_id"])] = resolved_path
    return resolved


def local_fallback_attachment_text(part: Mapping[str, Any]) -> str:
    """Render one persistent-safe attachment reference for a local media tool."""

    descriptor = {
        "attachment_id": str(part["attachment_id"]),
        "modality": str(part["modality"]),
        "mime_type": str(part["mime_type"]),
        "filename": str(part.get("filename") or ""),
        "caption": str(part.get("caption") or ""),
        "local_ref": str(part["local_ref"]),
    }
    return (
        "LOCAL_MEDIA_ATTACHMENT "
        + json.dumps(descriptor, ensure_ascii=False, separators=(",", ":"))
        + "\nThe media bytes were not sent natively. Use an authorized local "
        "media tool on local_ref before making any semantic claim about this "
        "attachment."
    )


def materialize_openai_user_content(
    prompt: str,
    request_content: Mapping[str, Any] | None,
    capability: InputCapability,
    *,
    authorized_roots: Sequence[str | Path],
    fallback_modalities: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], tuple[MediaRoutingDecision, ...]]:
    """Create transient native parts and ordered local-tool references."""

    normalized = normalize_request_content(request_content)
    prompt_text = str(prompt or "")
    text_part = {"type": "text", "text": prompt_text}
    if normalized is None:
        return [text_part], ()
    decisions = route_request_content(
        normalized,
        capability,
        fallback_modalities=fallback_modalities,
    )
    by_attachment = {item.attachment_id: item for item in decisions}
    native_ref_replacements = tuple(
        (
            str(part.get("local_ref") or ""),
            "[attachment "
            + str(part.get("attachment_id") or "unknown")
            + " supplied through native media input]",
        )
        for part in normalized["parts"]
        if part["type"] == "media"
        and by_attachment[part["attachment_id"]].route == "native"
        and str(part.get("local_ref") or "")
    )

    def scrub_native_refs(value: str) -> str:
        scrubbed = str(value or "")
        for local_ref, replacement in sorted(
            native_ref_replacements,
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            scrubbed = scrubbed.replace(local_ref, replacement)
            escaped_ref = json.dumps(local_ref, ensure_ascii=False)[1:-1]
            if escaped_ref != local_ref:
                scrubbed = scrubbed.replace(escaped_ref, replacement)
        return scrubbed

    text_part = {"type": "text", "text": scrub_native_refs(prompt_text)}
    canonical_text = "\n".join(
        str(part.get("text") or "")
        for part in normalized["parts"]
        if part["type"] == "text"
    )
    rendered: list[dict[str, Any]] = []
    if prompt_text and prompt_text != canonical_text:
        # Stage/provider instructions are not an original request item.  Keep
        # them ahead of the canonical subsequence without disturbing the
        # request's item_index order.
        rendered.append(text_part)
    for part in normalized["parts"]:
        if part["type"] == "text":
            rendered.append(
                {"type": "text", "text": scrub_native_refs(str(part["text"]))}
            )
            continue
        decision = by_attachment[part["attachment_id"]]
        if decision.route == "local_fallback":
            rendered.append(
                {"type": "text", "text": local_fallback_attachment_text(part)}
            )
            continue
        if decision.route != "native":
            continue
        mime_type = str(part["mime_type"])
        if part["modality"] == "image":
            if decision.transport != "data_url":
                raise MultimodalContractError(
                    "local image materialisation requires an explicitly supported data_url transport",
                    code="MEDIA_TRANSPORT_UNSUPPORTED",
                    attachment_id=str(part["attachment_id"]),
                )
            payload = _read_authorized_media(part, authorized_roots=authorized_roots)
            encoded = base64.b64encode(payload).decode("ascii")
            image_url: dict[str, Any] = {
                "url": f"data:{mime_type};base64,{encoded}"
            }
            if part.get("detail"):
                image_url["detail"] = part["detail"]
            rendered.append({"type": "image_url", "image_url": image_url})
            continue
        if part["modality"] == "audio":
            if decision.transport not in {"data_url", "inline"}:
                raise MultimodalContractError(
                    "local audio materialisation requires an inline transport",
                    code="MEDIA_TRANSPORT_UNSUPPORTED",
                    attachment_id=str(part["attachment_id"]),
                )
            payload = _read_authorized_media(part, authorized_roots=authorized_roots)
            encoded = base64.b64encode(payload).decode("ascii")
            audio_format = Path(str(part["filename"])).suffix.lstrip(".") or "wav"
            rendered.append(
                {
                    "type": "input_audio",
                    "input_audio": {"data": encoded, "format": audio_format},
                }
            )
            continue
        raise MultimodalContractError(
            f"no OpenAI-compatible transient shape is registered for {part['modality']}",
            code="MEDIA_TRANSPORT_UNSUPPORTED",
            attachment_id=str(part["attachment_id"]),
        )
    if not rendered:
        rendered.append(text_part)
    return rendered, decisions


def routing_decisions_payload(
    decisions: Iterable[MediaRoutingDecision],
) -> tuple[dict[str, Any], ...]:
    return tuple(item.as_dict() for item in decisions)
