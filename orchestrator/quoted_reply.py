"""Versioned, untrusted quote data for command-derived messages (Functions).

A reference never selects a Session, grants authority or supplies a command.
The command line and the authenticated invocation binding stay independent.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

_FIELDS = {"agent_id": 256, "session_id": 256, "message_ref": 512,
           "author": 240, "timestamp": 64, "text": 4000}
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_quoted_reply(value):
    allowed = {*_FIELDS, "role", "context_generation", "text_coverage", "source_complete"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError("invalid quote reference")
    coverage = value.get("text_coverage")
    has_coverage = "text_coverage" in value or "source_complete" in value
    if has_coverage and (
        coverage not in {"full", "excerpt"}
        or type(value.get("source_complete")) is not bool
        or (coverage == "full" and not value["source_complete"])
    ):
        raise ValueError("invalid quote coverage")
    result = {}
    for key, limit in _FIELDS.items():
        text = value.get(key, "")
        full_text = key == "text" and coverage == "full"
        if not isinstance(text, str) or (not full_text and len(text) > limit):
            raise ValueError("invalid quote reference")
        result[key] = text if full_text else _CONTROLS.sub("", text)
    if coverage != "full":
        result["text"] = result["text"].strip()
    generation = value.get("context_generation")
    role = value.get("role", "")
    if not result["text"].strip() or role not in {"", "user", "assistant"}:
        raise ValueError("invalid quote reference")
    if generation is not None and (type(generation) is not int or not 0 <= generation <= 9007199254740991):
        raise ValueError("invalid quote reference")
    if coverage == "full" and (
        not result["agent_id"] or not result["session_id"]
        or not result["message_ref"] or generation is None
    ):
        raise ValueError("full quote source identity missing")
    if coverage == "full" and any(
        result[key] != value.get(key) for key in ("agent_id", "session_id", "message_ref")
    ):
        raise ValueError("full quote source identity invalid")
    result.update(role=role, context_generation=generation)
    if has_coverage:
        result.update(text_coverage=coverage, source_complete=value["source_complete"])
    return result


def encode_quoted_reply(value, body):
    if value is None:
        return body
    quote = normalize_quoted_reply(value)
    version = "V2" if "text_coverage" in quote else "V1"
    return f"[[HASHI_QUOTE_{version}]]\n" + json.dumps(quote, ensure_ascii=False, separators=(",", ":")) + f"\n[[/HASHI_QUOTE_{version}]]\n" + body
# HASHI-Fingerprint: hfp_f23176d8299b4529919f321e14e70aea
