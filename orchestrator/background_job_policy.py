"""Request-scoped authority for starting managed background jobs."""

from __future__ import annotations

from typing import Any, Mapping


USER_BACKGROUND_JOB_REQUEST_SOURCE = "background:prompt"


def background_job_start_authorized(context: Mapping[str, Any] | None) -> bool:
    """Return whether this Agent turn came from an explicit user ``/bg`` request."""

    source = str((context or {}).get("request_source") or "").strip().casefold()
    return source == USER_BACKGROUND_JOB_REQUEST_SOURCE
