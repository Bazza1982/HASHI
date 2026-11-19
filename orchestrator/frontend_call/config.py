"""Instance-local allow-listed targets; revisioned per-owner/Agent selections."""

from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from orchestrator.config_json import (
    read_config_json,
    write_config_json,
    ConfigConflictError,
    ConfigDurabilityError,
    new_config_json,
)
from orchestrator.voice_catalog import VoiceCatalog, VoiceCatalogError
from orchestrator.lan_transport import lan_host_is_admissible as _lan_host_is_admissible
from .contract import CallError, digest, identifier

RESERVED = {
    "model",
    "input",
    "messages",
    "file",
    "stream",
    "response_format",
    "voice",
    "url",
    "api_key",
    "headers",
    "tools",
    "tool_choice",
    "provider",
}
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_GEMINI_TTS_MODELS = {
    "google/gemini-3.8-flash-lite-tts",
    "google/gemini-3.8-flash-tts",
}


_CALL_CAPACITY_SETTINGS = {
    "targets": "HASHI_CALL_MAX_TARGETS",
    "routes": "HASHI_CALL_MAX_ROUTES",
    "profiles": "HASHI_CALL_MAX_PROFILES",
    "agent_defaults": "HASHI_CALL_MAX_AGENT_DEFAULTS",
    "voices": "HASHI_CALL_MAX_VOICES",
    "options": "HASHI_CALL_MAX_OPTIONS",
    "model_chars": "HASHI_MEDIA_MAX_MODEL_CHARS",
}


def call_capacity():
    """Unset capacities impose no extra fixed catalogue count or model length."""
    result = {}
    for key, setting in _CALL_CAPACITY_SETTINGS.items():
        raw = os.environ.get(setting)
        if raw is None or raw == "":
            result[key] = None
        elif raw.isascii() and raw.isdecimal() and int(raw) > 0:
            result[key] = int(raw)
        else:
            raise CallError("call_capacity_invalid", 503)
    return result


def enforce_capacity(key, count, code, status=429):
    limit = call_capacity()[key]
    if limit is not None and count > limit:
        error = CallError(code, status)
        error.capacity = {"setting": _CALL_CAPACITY_SETTINGS[key], "limit": limit}
        raise error


def options_for(target, value):
    if not isinstance(value, dict):
        raise CallError("call_options_invalid")
    enforce_capacity("options", len(value), "call_options_limit")
    result = {}
    schema = target.get("options", {})
    for key, item in value.items():
        spec = schema.get(key)
        if key in RESERVED or not isinstance(spec, dict):
            raise CallError("call_option_unsupported")
        kind = spec.get("type")
        if kind == "string":
            maximum = spec.get("max_length", 300)
            if type(maximum) is not int or maximum < 0:
                raise CallError("call_option_schema_invalid", 503)
            if not isinstance(item, str) or len(item) > maximum:
                raise CallError("call_option_invalid")
        elif kind == "number":
            if (
                type(item) not in (int, float)
                or not math.isfinite(item)
                or not spec.get("min", -math.inf) <= item <= spec.get("max", math.inf)
            ):
                raise CallError("call_option_invalid")
        elif kind == "boolean":
            if type(item) is not bool:
                raise CallError("call_option_invalid")
        else:
            raise CallError("call_option_unsupported")
        if "enum" in spec and item not in spec["enum"]:
            raise CallError("call_option_invalid")
        result[key] = item
    return result


def pcm_settings_for(target):
    """Validate the exact raw PCM codec; recognize existing built-in Gemini defaults."""
    keys = (
        "pcm_sample_rate_hz",
        "pcm_sample_width_bytes",
        "pcm_channels",
        "pcm_encoding",
    )
    legacy = (
        target.get("kind") == "tts"
        and target.get("model") in OPENROUTER_GEMINI_TTS_MODELS
        and str(target.get("base_url") or "").rstrip("/") == OPENROUTER_API_BASE
        and not any(key in target for key in keys)
    )
    settings = (
        {
            "pcm_sample_rate_hz": 24000,
            "pcm_sample_width_bytes": 2,
            "pcm_channels": 1,
            "pcm_encoding": "s16le",
        }
        if legacy
        else {key: target.get(key) for key in keys}
    )
    rate = settings["pcm_sample_rate_hz"]
    width = settings["pcm_sample_width_bytes"]
    channels = settings["pcm_channels"]
    if (
        target.get("kind") != "tts"
        or type(rate) is not int
        or not 8000 <= rate <= 96000
        or type(width) is not int
        or width != 2
        or type(channels) is not int
        or channels not in (1, 2)
        or settings["pcm_encoding"] != "s16le"
    ):
        raise CallError("call_audio_format_unsupported", 503)
    return settings


def validate_target(target):
    if not isinstance(target, dict):
        raise CallError("call_configuration_invalid", 503)
    identifier(target.get("id"))
    if (
        target.get("kind") not in ("stt", "tts", "vision")
        or target.get("adapter") != "openai_compatible"
    ):
        raise CallError("call_adapter_unsupported", 503)
    if target.get("stt_protocol", "audio_transcriptions") not in (
        "audio_transcriptions",
        "audio_chat",
    ) or ("stt_protocol" in target and target["kind"] != "stt"):
        raise CallError("call_adapter_unsupported", 503)
    if "stt_segment_seconds" in target and (
        target["kind"] != "stt"
        or type(target["stt_segment_seconds"]) is not int
        or target["stt_segment_seconds"] < 1
    ):
        raise CallError("call_media_capacity_invalid", 503)
    parsed = urlsplit(str(target.get("base_url", "")))
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise CallError("call_target_invalid", 503)
    location = target.get("location")
    if location == "local":
        if parsed.scheme not in ("http", "https") or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise CallError("call_local_target_not_loopback", 503)
    elif location == "lan":
        if parsed.scheme not in ("http", "https") or not _lan_host_is_admissible(
            parsed.hostname
        ):
            raise CallError("call_lan_target_not_private", 503)
    elif location != "cloud" or parsed.scheme != "https":
        raise CallError("call_target_invalid", 503)
    if not isinstance(target.get("model"), str) or not target["model"].strip():
        raise CallError("call_model_required", 503)
    enforce_capacity("model_chars", len(target["model"]), "call_model_limit", 503)
    credential = target.get("credential_env", "")
    if credential and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", credential):
        raise CallError("call_credential_reference_invalid", 503)
    credential_ref = target.get("credential_ref", "")
    if credential_ref and (
        credential
        or not isinstance(credential_ref, str)
        or not re.fullmatch(
            r"(?:env://[A-Z][A-Z0-9_]{0,100}|secrets://[A-Za-z0-9][A-Za-z0-9_-]{0,100})",
            credential_ref,
        )
    ):
        raise CallError("call_credential_reference_invalid", 503)
    if target["kind"] == "tts" and (
        not isinstance(target.get("voices"), list) or not target["voices"]
    ):
        raise CallError("call_voices_required", 503)
    voices = target.get("voices", [])
    if not isinstance(voices, list):
        raise CallError("call_voices_invalid", 503)
    enforce_capacity("voices", len(voices), "call_voices_limit", 503)
    for voice in voices:
        if not isinstance(voice, str) or not 1 <= len(voice) <= 160:
            raise CallError("call_voices_invalid", 503)
    if len(set(voices)) != len(voices):
        raise CallError("call_voices_invalid", 503)
    voice_styles = target.get("voice_styles", {})
    if (
        not isinstance(voice_styles, dict)
        or set(voice_styles) - set(target.get("voices", []))
        or any(
            not isinstance(label, str) or not 1 <= len(label) <= 50
            for label in voice_styles.values()
        )
    ):
        raise CallError("call_voices_invalid", 503)
    audio_format = target.get("audio_format", "mp3")
    if audio_format not in ("mp3", "wav", "pcm"):
        raise CallError("call_audio_format_unsupported", 503)
    pcm_settings = pcm_settings_for(target) if audio_format == "pcm" else {}
    schema = target.get("options", {})
    if not isinstance(schema, dict) or set(schema) & RESERVED:
        raise CallError("call_option_schema_invalid", 503)
    enforce_capacity("options", len(schema), "call_option_schema_limit", 503)
    for key, spec in schema.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", key)
            or not isinstance(spec, dict)
        ):
            raise CallError("call_option_schema_invalid", 503)
        if set(spec) - {"type", "min", "max", "max_length", "enum"} or spec.get(
            "type"
        ) not in ("string", "number", "boolean"):
            raise CallError("call_option_schema_invalid", 503)
        if "max_length" in spec and (
            type(spec["max_length"]) is not int or spec["max_length"] < 0
        ):
            raise CallError("call_option_schema_invalid", 503)
        for bound in ("min", "max"):
            if bound in spec and (
                type(spec[bound]) not in (int, float) or not math.isfinite(spec[bound])
            ):
                raise CallError("call_option_schema_invalid", 503)
        if spec.get("min", -math.inf) > spec.get("max", math.inf):
            raise CallError("call_option_schema_invalid", 503)
        if "enum" in spec:
            if not isinstance(spec["enum"], list):
                raise CallError("call_option_schema_invalid", 503)
            kind = spec["type"]
            for item in spec["enum"]:
                if kind == "string":
                    valid = isinstance(item, str) and len(item) <= spec.get(
                        "max_length", 300
                    )
                elif kind == "number":
                    valid = (
                        type(item) in (int, float)
                        and math.isfinite(item)
                        and spec.get("min", -math.inf)
                        <= item
                        <= spec.get("max", math.inf)
                    )
                else:
                    valid = type(item) is bool
                if not valid:
                    raise CallError("call_option_schema_invalid", 503)
    return {**copy.deepcopy(target), **pcm_settings}


class CallConfig:
    def __init__(
        self,
        path,
        *,
        voice_catalog_path=None,
        project_root=None,
        platform=None,
    ):
        self.path = Path(path)
        self.voice_catalog_path = (
            Path(voice_catalog_path) if voice_catalog_path is not None else None
        )
        self.project_root = Path(project_root or self.path.parent)
        self.platform = platform

    def _voice_catalog(self):
        if self.voice_catalog_path is None:
            return None
        try:
            return VoiceCatalog.load(
                instance_path=self.voice_catalog_path,
                project_root=self.project_root,
                platform=self.platform,
            )
        except VoiceCatalogError as exc:
            error = CallError("call_tts_catalog_invalid", 503)
            if isinstance(getattr(exc, "capacity", None), dict):
                error.capacity = dict(exc.capacity)
            raise error from exc

    @staticmethod
    def _catalog_target(catalog, service_id):
        service = catalog.service(service_id)
        voice_ids = list(catalog.voice_ids(service_id))
        metadata = {
            voice_id: {
                key: copy.deepcopy(value)
                for key, value in catalog.preset(voice_id).items()
                if key
                in {
                    "label",
                    "display_names",
                    "language",
                    "language_labels",
                    "locale",
                    "gender",
                    "style",
                    "voice_name",
                    "provider_options",
                }
            }
            for voice_id in voice_ids
        }
        return {
            "id": service_id,
            "kind": "tts",
            "label": service["label"],
            "menu_label": service["menu_label"],
            "adapter": service["adapter"],
            "location": service["location"],
            "model": service.get("model") or service["adapter"],
            "base_url": service.get("base_url"),
            "audio_format": service.get("audio_format") or "ogg",
            "voices": voice_ids,
            "voice_metadata": metadata,
            "fallbacks": list(catalog.fallbacks(service_id)),
            "options": {},
            "catalog_revision": catalog.revision,
        }

    def _read_bundle(self):
        try:
            doc = read_config_json(self.path)
        except FileNotFoundError as exc:
            raise CallError("call_not_configured", 503) from exc
        except Exception as exc:
            raise CallError("call_configuration_invalid", 503) from exc
        if doc.get("version") != 1 or doc.get("enabled") is not True:
            raise CallError("call_disabled", 503)
        rows = doc.get("targets", [])
        if not isinstance(rows, list) or not rows:
            raise CallError("call_targets_invalid", 503)
        if any(not isinstance(row, dict) for row in rows):
            raise CallError("call_configuration_invalid", 503)
        catalog = self._voice_catalog()
        selected_rows = (
            rows
            if catalog is None
            else [row for row in rows if row.get("kind") != "tts"]
        )
        targets = {row["id"]: row for row in map(validate_target, selected_rows)}
        if len(targets) != len(selected_rows):
            raise CallError("call_duplicate_target", 503)
        if catalog is not None:
            for service_id in catalog.service_ids:
                if not catalog.voice_ids(service_id):
                    continue
                if service_id in targets:
                    raise CallError("call_duplicate_target", 503)
                targets[service_id] = self._catalog_target(catalog, service_id)
            if not any(row["kind"] == "tts" for row in targets.values()):
                raise CallError("call_tts_catalog_empty", 503)
        enforce_capacity("targets", len(targets), "call_targets_limit", 503)
        revision = (
            doc.revision
            if catalog is None
            else digest([doc.revision, catalog.revision])
        )
        return doc, targets, catalog, revision

    def initialize(self):
        """Create an absent declaration during setup, never replace a saved choice."""
        try:
            read_config_json(self.path)
        except FileNotFoundError:
            from .defaults import default_call_configuration

            self._write(
                new_config_json(
                    self.path,
                    default_call_configuration(voice_catalog=self._voice_catalog()),
                )
            )
        except Exception:
            # A bad optional media declaration fails at the call boundary,
            # without taking the authenticated chat/command API down with it.
            return

    def route(self, owner, agent, doc=None, targets=None):
        """Project independent /call readiness and the legacy client choice."""
        revision = None
        if doc is None:
            try:
                doc = read_config_json(self.path)
            except FileNotFoundError:
                return {
                    "route": "phone",
                    "camera_available": False,
                    "call_ready": False,
                }
            except Exception as exc:
                raise CallError("call_configuration_invalid", 503) from exc
            if doc.get("enabled") is True:
                doc, targets, _catalog, revision = self._read_bundle()
        # New and future (owner, agent) pairs default to the /call entrance;
        # explicitly saved "phone" selections remain authoritative.
        route = doc.get("routes", {}).get(digest([owner, agent]), "call")
        if route not in ("phone", "call"):
            raise CallError("call_configuration_invalid", 503)
        ready = doc.get("enabled") is True
        if not ready:
            route = "phone"
        camera = False
        if ready:
            if targets is None:
                targets = {
                    row["id"]: row
                    for row in map(validate_target, doc.get("targets", []))
                }
            camera = self.profile(owner, agent, doc, targets).get("vision") is not None
        return {
            "route": route,
            "camera_available": camera,
            "call_ready": ready,
            "revision": revision or doc.revision,
        }

    def select_route(self, owner, agent, revision, route):
        if route not in ("phone", "call"):
            raise CallError("call_route_invalid")
        if route == "phone":
            try:
                doc = read_config_json(self.path)
            except Exception as exc:
                raise CallError("call_configuration_invalid", 503) from exc
            if doc.get("version") != 1:
                raise CallError("call_configuration_invalid", 503)
            if doc.get("enabled") is True:
                doc, _targets, _catalog, effective_revision = self._read_bundle()
            else:
                effective_revision = doc.revision
        else:
            doc, _targets, _catalog, effective_revision = self._read_bundle()
        if effective_revision != revision:
            raise CallError("call_configuration_changed", 409)
        routes = doc.setdefault("routes", {})
        if not isinstance(routes, dict):
            raise CallError("call_configuration_invalid", 503)
        key = digest([owner, agent])
        if key not in routes:
            enforce_capacity("routes", len(routes) + 1, "call_profile_limit")
        routes[key] = route
        self._write(doc)
        return self.route(owner, agent)

    def video_policy(self, doc=None):
        if doc is None:
            doc, _ = self.read()
        policy = {
            "interval_ms": 500,
            "freshness_seconds": 20,
            "responsive_seconds": 12,
            "startup_timeout_seconds": 8,
            "max_observations_per_minute": 120,
            "sample_window_seconds": 10,
            "max_frames_per_turn": 10,
            "max_buffer_frames": 32,
            "max_buffer_bytes": 0,
        }
        supplied = doc.get("video_policy", {})
        if not isinstance(supplied, dict) or set(supplied) - set(policy):
            raise CallError("call_video_policy_invalid", 503)
        policy.update(supplied)
        # These are selected resource budgets, not universal eligibility
        # rules. Keep integer validation while allowing the instance's chosen
        # slower device and freshness policy to reach every Call consumer.
        if any(
            type(value) is not int or not 1 <= value <= 2**31 - 1
            for key, value in policy.items()
            if key != "max_buffer_bytes"
        ):
            raise CallError("call_video_policy_invalid", 503)
        if (
            type(policy["max_buffer_bytes"]) is not int
            or not 0 <= policy["max_buffer_bytes"] <= 2**53 - 1
        ):
            raise CallError("call_video_policy_invalid", 503)
        # Older configurations may omit a responsive window.
        if "responsive_seconds" not in supplied:
            policy["responsive_seconds"] = min(
                policy["responsive_seconds"], policy["freshness_seconds"]
            )
        if policy["responsive_seconds"] > policy["freshness_seconds"]:
            raise CallError("call_video_policy_invalid", 503)
        return policy

    def _write(self, doc):
        try:
            write_config_json(self.path, doc)
        except ConfigConflictError as exc:
            raise CallError("call_configuration_changed", 409) from exc
        except ConfigDurabilityError as exc:
            raise CallError("call_configuration_outcome_unknown", 503) from exc

    def read(self):
        doc, targets, _catalog, _revision = self._read_bundle()
        return doc, targets

    def profile(self, owner, agent, doc=None, targets=None):
        if doc is None:
            doc, targets = self.read()
        key = digest([owner, agent])
        agent_defaults = doc.get("agent_defaults", {})
        if not isinstance(agent_defaults, dict):
            raise CallError("call_profile_invalid")
        enforce_capacity("agent_defaults", len(agent_defaults), "call_profile_limit")
        default = agent_defaults.get(agent, doc.get("default_profile", {}))
        value = doc.get("profiles", {}).get(key, default)
        return self.validate_profile(value, targets)

    @staticmethod
    def validate_profile(value, targets):
        if not isinstance(value, dict) or set(value) - {
            "stt",
            "tts",
            "vision",
            "manual_direction",
        }:
            raise CallError("call_profile_invalid")
        result = {}
        if "manual_direction" in value:
            from orchestrator.manual_speech_direction import validate

            result["manual_direction"] = validate(value["manual_direction"])
        for kind in ("stt", "tts", "vision"):
            slot = value.get(kind)
            if kind == "vision" and slot is None:
                result[kind] = None
                continue
            if not isinstance(slot, dict) or set(slot) - {
                "target_id",
                "voice_id",
                "options",
            }:
                raise CallError("call_profile_invalid")
            target = targets.get(slot.get("target_id"))
            if not target or target["kind"] != kind:
                raise CallError("call_target_unavailable", 409)
            selected = {
                "target_id": target["id"],
                "options": options_for(target, slot.get("options", {})),
            }
            if kind == "tts":
                if slot.get("voice_id") not in target["voices"]:
                    raise CallError("call_voice_unavailable", 409)
                selected["voice_id"] = slot["voice_id"]
            elif "voice_id" in slot:
                raise CallError("call_option_unsupported")
            result[kind] = selected
        return result

    def context(self, owner, agent):
        try:
            disabled = read_config_json(self.path)
        except FileNotFoundError as exc:
            raise CallError("call_not_configured", 503) from exc
        except Exception as exc:
            raise CallError("call_configuration_invalid", 503) from exc
        if disabled.get("version") == 1 and disabled.get("enabled") is not True:
            return {
                "revision": disabled.revision,
                "targets": [],
                "capacity": call_capacity(),
                "profile": {"stt": None, "tts": None, "vision": None},
                "video_policy": self.video_policy(disabled),
                **self.route(owner, agent, disabled),
            }
        doc, targets, _catalog, revision = self._read_bundle()
        public = [
            {
                k: copy.deepcopy(v)
                for k, v in row.items()
                if k
                in {
                    "id",
                    "label",
                    "menu_label",
                    "kind",
                    "adapter",
                    "location",
                    "model",
                    "stt_segment_seconds",
                    "voices",
                    "voice_metadata",
                    "voice_styles",
                    "fallbacks",
                    "catalog_revision",
                    "options",
                }
            }
            for row in targets.values()
        ]
        return {
            "revision": revision,
            "targets": public,
            "capacity": call_capacity(),
            "profile": self.profile(owner, agent, doc, targets),
            "video_policy": self.video_policy(doc),
            **{
                key: value
                for key, value in self.route(owner, agent, doc, targets).items()
                if key != "revision"
            },
        }

    def video_tts_selection(self, revision):
        """Freeze Call's exact VideoTTS reference from the same read revision."""
        from .video_tts import VideoTTSSelection

        doc, _targets, _catalog, effective_revision = self._read_bundle()
        if revision != effective_revision:
            raise CallError("call_configuration_changed", 409)
        return VideoTTSSelection.parse(doc.get("video_tts"))

    def freeze(self, owner, agent, revision):
        profile, selected, _catalog = self.freeze_runtime(owner, agent, revision)
        return profile, selected

    def freeze_runtime(self, owner, agent, revision):
        doc, targets, catalog, effective_revision = self._read_bundle()
        if revision != effective_revision:
            raise CallError("call_configuration_changed", 409)
        profile = self.profile(owner, agent, doc, targets)
        selected = {
            kind: copy.deepcopy(targets[slot["target_id"]]) if slot else None
            for kind, slot in profile.items()
            if kind in ("stt", "tts", "vision")
        }
        return profile, selected, catalog

    def save(self, owner, agent, revision, profile):
        doc, targets, _catalog, effective_revision = self._read_bundle()
        if revision != effective_revision:
            raise CallError("call_configuration_changed", 409)
        profile = self.validate_profile(profile, targets)
        profiles = doc.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            raise CallError("call_profile_invalid")
        key = digest([owner, agent])
        if key not in profiles:
            enforce_capacity("profiles", len(profiles) + 1, "call_profile_limit")
        profiles[key] = profile
        self._write(doc)
        return self.context(owner, agent)


# HASHI-Fingerprint: hfp_e30d01328e8349c7b2bc8973641ce276
