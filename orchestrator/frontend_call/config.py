"""Instance-local allow-listed targets; revisioned per-owner/Agent selections."""

from __future__ import annotations

import copy
import math
import re
from urllib.parse import urlsplit
from orchestrator.config_json import (
    read_config_json,
    write_config_json,
    ConfigConflictError,
    ConfigDurabilityError,
)
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
}


def options_for(target, value):
    if not isinstance(value, dict) or len(value) > 16:
        raise CallError("call_options_invalid")
    result = {}
    schema = target.get("options", {})
    for key, item in value.items():
        spec = schema.get(key)
        if key in RESERVED or not isinstance(spec, dict):
            raise CallError("call_option_unsupported")
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(item, str) or len(item) > min(
                int(spec.get("max_length", 300)), 1000
            ):
                raise CallError("call_option_invalid")
        elif kind == "number":
            if (
                type(item) not in (int, float)
                or not math.isfinite(item)
                or not spec.get("min", -100) <= item <= spec.get("max", 100)
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


def validate_target(target):
    if not isinstance(target, dict):
        raise CallError("call_configuration_invalid", 503)
    identifier(target.get("id"))
    if (
        target.get("kind") not in ("stt", "tts", "vision")
        or target.get("adapter") != "openai_compatible"
    ):
        raise CallError("call_adapter_unsupported", 503)
    parsed = urlsplit(str(target.get("base_url", "")))
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise CallError("call_target_invalid", 503)
    if target.get("location") == "local":
        if parsed.scheme not in ("http", "https") or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise CallError("call_local_target_not_loopback", 503)
    elif target.get("location") != "cloud" or parsed.scheme != "https":
        raise CallError("call_target_invalid", 503)
    if not isinstance(target.get("model"), str) or not 1 <= len(target["model"]) <= 200:
        raise CallError("call_model_required", 503)
    credential = target.get("credential_env", "")
    if credential and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", credential):
        raise CallError("call_credential_reference_invalid", 503)
    if target["kind"] == "tts" and (
        not isinstance(target.get("voices"), list)
        or not target["voices"]
        or len(target["voices"]) > 200
    ):
        raise CallError("call_voices_required", 503)
    for voice in target.get("voices", []):
        if not isinstance(voice, str) or not 1 <= len(voice) <= 160:
            raise CallError("call_voices_invalid", 503)
    if target.get("audio_format", "mp3") not in ("mp3", "wav"):
        raise CallError("call_audio_format_unsupported", 503)
    schema = target.get("options", {})
    if not isinstance(schema, dict) or len(schema) > 16 or set(schema) & RESERVED:
        raise CallError("call_option_schema_invalid", 503)
    for key, spec in schema.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", key) or not isinstance(spec, dict):
            raise CallError("call_option_schema_invalid", 503)
        if set(spec) - {"type", "min", "max", "max_length", "enum"} or spec.get(
            "type"
        ) not in ("string", "number", "boolean"):
            raise CallError("call_option_schema_invalid", 503)
    return copy.deepcopy(target)


class CallConfig:
    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            doc = read_config_json(self.path)
        except FileNotFoundError as exc:
            raise CallError("call_not_configured", 503) from exc
        except Exception as exc:
            raise CallError("call_configuration_invalid", 503) from exc
        if doc.get("version") != 1 or doc.get("enabled") is not True:
            raise CallError("call_disabled", 503)
        rows = doc.get("targets", [])
        if not isinstance(rows, list) or not 1 <= len(rows) <= 30:
            raise CallError("call_targets_invalid", 503)
        targets = {row["id"]: row for row in map(validate_target, rows)}
        if len(targets) != len(rows):
            raise CallError("call_duplicate_target", 503)
        return doc, targets

    def profile(self, owner, agent, doc=None, targets=None):
        if doc is None:
            doc, targets = self.read()
        key = digest([owner, agent])
        value = doc.get("profiles", {}).get(key, doc.get("default_profile", {}))
        return self.validate_profile(value, targets)

    @staticmethod
    def validate_profile(value, targets):
        if not isinstance(value, dict) or set(value) - {"stt", "tts", "vision"}:
            raise CallError("call_profile_invalid")
        result = {}
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
        doc, targets = self.read()
        public = [
            {
                k: copy.deepcopy(v)
                for k, v in row.items()
                if k
                in {
                    "id",
                    "label",
                    "kind",
                    "adapter",
                    "location",
                    "model",
                    "voices",
                    "options",
                }
            }
            for row in targets.values()
        ]
        return {
            "revision": doc.revision,
            "targets": public,
            "profile": self.profile(owner, agent, doc, targets),
        }

    def freeze(self, owner, agent, revision):
        doc, targets = self.read()
        if revision != doc.revision:
            raise CallError("call_configuration_changed", 409)
        profile = self.profile(owner, agent, doc, targets)
        selected = {
            kind: copy.deepcopy(targets[slot["target_id"]]) if slot else None
            for kind, slot in profile.items()
        }
        return profile, selected

    def save(self, owner, agent, revision, profile):
        doc, targets = self.read()
        if revision != doc.revision:
            raise CallError("call_configuration_changed", 409)
        profile = self.validate_profile(profile, targets)
        profiles = doc.setdefault("profiles", {})
        if len(profiles) >= 200 and digest([owner, agent]) not in profiles:
            raise CallError("call_profile_limit", 429)
        profiles[digest([owner, agent])] = profile
        try:
            write_config_json(self.path, doc)
        except ConfigConflictError as exc:
            raise CallError("call_configuration_changed", 409) from exc
        except ConfigDurabilityError as exc:
            raise CallError("call_configuration_outcome_unknown", 503) from exc
        return self.context(owner, agent)
