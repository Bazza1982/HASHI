"""The fixed non-network capability needed by the sealed Windows Python runtime.

Windows' SxS loader reads its registry configuration. LPAC blocks that without
registryRead; this does not grant registry write, network or a file capability.
The SID is Windows' immutable DeriveCapabilitySidsFromName("registryRead") result.
No request, model or installation payload may choose extra capabilities.
"""
from __future__ import annotations

from collections.abc import Mapping

PYTHON_REGISTRY_READ_SID = (
    "S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-432734479-"
    "3232135806-4053264122-3456934681"
)
PYTHON_RUNTIME_CAPABILITIES = (PYTHON_REGISTRY_READ_SID,)


def python_runtime_capabilities(observation):
    if isinstance(observation, Mapping):
        count = observation.get("capability_count")
        values = observation.get("capability_sids")
    else:
        count = getattr(observation, "capability_count", None)
        values = getattr(observation, "capability_sids", None)
    return (type(count) is int and count == 1 and isinstance(values, (tuple, list))
            and tuple(values) == PYTHON_RUNTIME_CAPABILITIES)
