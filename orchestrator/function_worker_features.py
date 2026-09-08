"""Optional Function Worker capabilities negotiated in the bootstrap envelope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

WORKER_LOG_RELAY_FEATURE = "worker-log-relay-v1"


def worker_log_relay_enabled(bootstrap: Mapping[str, Any]) -> bool:
    """Return whether the creating supervisor explicitly accepts log events.

    Bootstrap capabilities are additive and fail closed.  Missing, malformed,
    and unknown feature declarations therefore preserve legacy Supervisor
    behavior instead of enabling a Worker event that peer cannot decode.
    """
    features = bootstrap.get("protocol_features")
    if (
        not isinstance(features, (list, tuple))
        or not all(isinstance(feature, str) for feature in features)
    ):
        return False
    return WORKER_LOG_RELAY_FEATURE in features
