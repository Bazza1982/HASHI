"""PAO publication policy for the root Agent declaration.

The JSON primitive owns revisions/publication; this owner supplies only the
read-only Windows identity explicitly pinned in the installed service descriptor.
It never copies an existing DACL or shares secrets.json with the authority.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from orchestrator.config_json import ConfigList, write_config_json

_ACCOUNT_SID = re.compile(r"S-1-5-(?:21-(?:[0-9]+-){3}[0-9]+|80-(?:[0-9]+-){4}[0-9]+)\Z")


def write_agent_config_json(path: str | Path, document: dict | ConfigList, **publication):
    readers = ()
    if os.name == "nt" and isinstance(document, dict):
        global_settings = document.get("global")
        descriptor = global_settings.get("permission_service") if isinstance(global_settings, dict) else None
        if isinstance(descriptor, dict) and descriptor.get("service_sid") is not None:
            sid = descriptor["service_sid"]
            if not isinstance(sid, str) or _ACCOUNT_SID.fullmatch(sid) is None:
                raise ValueError("permission service requires an explicit private account SID")
            readers = (sid,)
    if readers:
        return write_config_json(path, document, private_read_sids=readers, **publication)
    return write_config_json(path, document, **publication)
# HASHI-Fingerprint: hfp_d36e55406d714582ba41a7347b9f6141
