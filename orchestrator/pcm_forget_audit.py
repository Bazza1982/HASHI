"""Route retained forget evidence through HASHI's canonical audit owner."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_SINK: ContextVar[Any] = ContextVar("pcm_forget_audit_sink", default=None)


@contextmanager
def audit_scope(sink: Any):
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def payload_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def protect(kind: str, identity: str, value: Any) -> Any:
    sink = _SINK.get()
    if sink is None:
        # Low-level isolated tests and legacy callers retain the original form.
        # The user-facing command requires the canonical sink before admission.
        return value
    record = sink.record(
        "pcm_forget.retained",
        {"kind": str(kind), "identity": str(identity), "value": value},
        provenance={"component": "pcm_forget"},
    )
    return {"canonical_audit_ref": record, "sha256": payload_digest(value)}
