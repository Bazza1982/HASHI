"""Private Profile owner using the native revision-checked configuration writer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from orchestrator.config_json import (
    ConfigConflictError, ConfigDurabilityError, new_config_json,
    read_config_json, write_config_json,
)
from .contract import StandbyError, identifier, normalize_profile

MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_PROFILES = 1024
MAX_SCOPE_PROFILES = 16


def _scope(owner, agent):
    return hashlib.sha256((str(owner) + "\0" + identifier(agent, maximum=64)).encode("utf-8")).hexdigest()


class StandbyProfiles:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("size")
            document = read_config_json(self.path) if self.path.exists() else new_config_json(self.path, {"schema_version": 1, "scopes": {}})
            if (document.get("schema_version") != 1 or not isinstance(document.get("scopes"), dict)
                    or sum(len(items) for items in document["scopes"].values() if isinstance(items, dict)) > MAX_PROFILES
                    or any(not isinstance(items, dict) for items in document["scopes"].values())):
                raise ValueError("schema")
            return document
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            raise StandbyError("standby_configuration_invalid", 503) from error

    def list(self, owner, agent):
        document = self._read()
        items = document["scopes"].get(_scope(owner, agent), {})
        return {"revision": document.revision,
                "profiles": [{"profile_id": key, "profile": value} for key, value in sorted(items.items())]}

    def get(self, owner, agent, profile_id):
        profile_id = identifier(profile_id, maximum=64)
        listing = self.list(owner, agent)
        for row in listing["profiles"]:
            if row["profile_id"] == profile_id:
                # A malformed saved document is blocked, never silently repaired.
                saved = dict(row["profile"])
                if saved.pop("schema_version", None) != 1:
                    raise StandbyError("standby_configuration_invalid", 503)
                try:
                    parsed = normalize_profile(saved)
                except (StandbyError, TypeError, ValueError) as error:
                    raise StandbyError("standby_configuration_invalid", 503) from error
                return {"revision": listing["revision"], "profile_id": profile_id, "profile": parsed}
        raise StandbyError("standby_profile_not_found", 404)

    def save(self, owner, agent, profile_id, profile, *, expected_revision):
        profile_id = identifier(profile_id, maximum=64)
        parsed = normalize_profile(profile)
        document = self._read()
        if document.revision != expected_revision:
            raise StandbyError("standby_revision_conflict", 409)
        scope = _scope(owner, agent)
        items = dict(document["scopes"].get(scope, {}))
        count = sum(len(values) for values in document["scopes"].values())
        if profile_id not in items and (len(items) >= MAX_SCOPE_PROFILES or count >= MAX_PROFILES):
            raise StandbyError("standby_capacity_reached", 429)
        items[profile_id] = parsed
        document["scopes"][scope] = items
        if len(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")) + 1 > MAX_CONFIG_BYTES:
            raise StandbyError("standby_capacity_reached", 429)
        try:
            current_revision = write_config_json(self.path, document)
        except ConfigConflictError as error:
            raise StandbyError("standby_revision_conflict", 409) from error
        except ConfigDurabilityError as error:
            # Publication may have committed: re-read the same file before any retry.
            raise StandbyError("standby_configuration_outcome_unknown", 503) from error
        except (OSError, ValueError) as error:
            raise StandbyError("standby_configuration_unavailable", 503) from error
        return {"revision": current_revision, "profile_id": profile_id, "profile": parsed}
