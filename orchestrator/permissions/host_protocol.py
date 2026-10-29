"""Closed transport for the existing HER configuration and results.

Only the trusted PAO/runner supplies a host command. No Python pickle, import
name, callable, path loader or model-provided class is accepted by this codec.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
import json

from orchestrator.her_v2.config import HERv2Config, ProviderProfile, ProviderTarget
from orchestrator.her_v2.models import DeliveryRecord, Effort, Route, Stage, TerminalState, TriageClassification, TurnResult
from orchestrator.her_v2.v3_config import CompanionConfig

_CLASSES = {c.__name__: c for c in (HERv2Config, ProviderProfile, ProviderTarget, CompanionConfig,
                                     TurnResult, DeliveryRecord)}
_ENUMS = {c.__name__: c for c in (Effort, Route, Stage, TerminalState, TriageClassification)}


def pack(value, *, _depth=0):
    if _depth > 64:
        raise ValueError("HER host value is too deep")
    def sub(item):
        return pack(item, _depth=_depth + 1)

    if isinstance(value, Enum):
        if _ENUMS.get(type(value).__name__) is not type(value):
            raise ValueError("unknown HER host enum")
        return ["enum", type(value).__name__, value.value]
    if is_dataclass(value) and not isinstance(value, type):
        if _CLASSES.get(type(value).__name__) is not type(value):
            raise ValueError("unknown HER host class")
        return ["dto", type(value).__name__, [[f.name, sub(getattr(value, f.name))] for f in fields(value)]]
    if isinstance(value, Mapping):
        return ["map", [[sub(key), sub(item)] for key, item in value.items()]]
    if isinstance(value, (tuple, list)):
        return ["tuple" if isinstance(value, tuple) else "list", [sub(item) for item in value]]
    if value is None or type(value) in (str, bool, int, float):
        json.dumps(value, allow_nan=False)
        return ["scalar", value]
    raise ValueError("unsupported HER host value")


def unpack(value, *, _depth=0):
    if _depth > 64 or not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise ValueError("invalid HER host value")
    kind = value[0]
    def sub(item):
        return unpack(item, _depth=_depth + 1)

    if kind == "scalar" and len(value) == 2:
        item = value[1]
        if item is None or type(item) in (str, bool, int, float):
            json.dumps(item, allow_nan=False)
            return item
    if kind in {"list", "tuple", "map"} and len(value) == 2 and isinstance(value[1], list):
        if kind != "map":
            items = [sub(item) for item in value[1]]
            return tuple(items) if kind == "tuple" else items
        result = {}
        for pair in value[1]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("invalid HER host map entry")
            key, item = sub(pair[0]), sub(pair[1])
            if not isinstance(key, (str, int)) or isinstance(key, bool) or key in result:
                raise ValueError("invalid or duplicate HER host map key")
            result[key] = item
        return result
    if kind == "enum" and len(value) == 3 and isinstance(value[1], str) and value[1] in _ENUMS:
        return _ENUMS[value[1]](value[2])
    if kind == "dto" and len(value) == 3 and isinstance(value[1], str) and value[1] in _CLASSES:
        cls = _CLASSES[value[1]]
        if not isinstance(value[2], list):
            raise ValueError("invalid HER host DTO")
        values = {}
        for pair in value[2]:
            if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                    or pair[0] in values):
                raise ValueError("invalid HER host DTO fields")
            values[pair[0]] = sub(pair[1])
        if set(values) != {f.name for f in fields(cls)}:
            raise ValueError("HER host DTO schema changed")
        return cls(**values)
    raise ValueError("unknown HER host value")
