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
    if not isinstance(value, Mapping) or set(value) - {*_FIELDS, "role", "context_generation"}:
        raise ValueError("invalid quote reference")
    result = {}
    for key, limit in _FIELDS.items():
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > limit:
            raise ValueError("invalid quote reference")
        result[key] = _CONTROLS.sub("", text)
    result["text"] = result["text"].strip()
    generation = value.get("context_generation")
    role = value.get("role", "")
    if not result["text"] or role not in {"", "user", "assistant"}:
        raise ValueError("invalid quote reference")
    if generation is not None and (type(generation) is not int or not 0 <= generation <= 9007199254740991):
        raise ValueError("invalid quote reference")
    result.update(role=role, context_generation=generation)
    return result


def encode_quoted_reply(value, body):
    if value is None:
        return body
    quote = normalize_quoted_reply(value)
    return "[[HASHI_QUOTE_V1]]\n" + json.dumps(quote, ensure_ascii=False, separators=(",", ":")) + "\n[[/HASHI_QUOTE_V1]]\n" + body
# HASHI-Fingerprint: hfp_f23176d8299b4529919f321e14e70aea
