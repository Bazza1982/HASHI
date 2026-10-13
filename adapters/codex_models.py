"""Read the model catalogue maintained by the installed Codex CLI.

The CLI inherits CODEX_HOME; use that same home without reading credentials or
starting a model request. This observation augments HASHI's qualified baseline,
not the Engine grants or an explicit per-Agent model restriction.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_MAX_CACHE_BYTES = 4 * 1024 * 1024


@lru_cache(maxsize=8)
def _read_catalogue(path: Path, mtime_ns: int, size: int) -> tuple:
    del mtime_ns, size  # The file revision is part of the cache key.
    try:
        with path.open("rb") as stream:
            content = stream.read(_MAX_CACHE_BYTES + 1)
        if len(content) > _MAX_CACHE_BYTES:
            return ()
        payload = json.loads(content.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return ()
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return ()
    choices = {}
    for row in payload["models"]:
        if not isinstance(row, dict) or row.get("visibility") != "list":
            continue
        model = row.get("slug")
        if not isinstance(model, str) or not model.strip():
            continue
        levels = row.get("supported_reasoning_levels")
        efforts = None
        if isinstance(levels, list) and all(
            isinstance(level, dict) and isinstance(level.get("effort"), str)
            and level["effort"].strip() for level in levels
        ):
            efforts = tuple(dict.fromkeys(level["effort"].strip().lower() for level in levels))
        choices[model.strip()] = efforts
    return tuple(choices.items())


def native_model_catalogue() -> dict[str, tuple[str, ...] | None]:
    configured = os.environ.get("CODEX_HOME", "").strip()
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    # A relative override would depend on the adapter's per-Agent cwd. Do not
    # accidentally expose choices observed under a different CLI home.
    if not home.is_absolute():
        return {}
    path = home / "models_cache.json"
    try:
        stat = path.stat()
    except OSError:
        return {}
    if stat.st_size > _MAX_CACHE_BYTES:
        return {}
    return dict(_read_catalogue(path, stat.st_mtime_ns, stat.st_size))
