from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from .shared_token import build_auth_headers, canonical_request_target


def build_client_auth_headers(
    *,
    url: str,
    method: str,
    data: bytes | None,
    token: str | None,
    shared_token: str | None,
    from_instance: str | None,
    normalize_instance: Callable[[str | None], str | None],
    load_default_instance: Callable[[], str | None] | None = None,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return headers
    if shared_token:
        sender = normalize_instance(from_instance)
        if not sender and load_default_instance is not None:
            sender = normalize_instance(load_default_instance())
        if not sender:
            raise ValueError("shared-token mode requires a sender instance id")
        split = urlsplit(url)
        headers.update(
            build_auth_headers(
                shared_token=shared_token,
                method=method,
                path=canonical_request_target(split.path, split.query),
                from_instance=sender,
                body_bytes=data or b"",
            )
        )
    return headers
