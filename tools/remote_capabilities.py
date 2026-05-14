from __future__ import annotations

import json
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


def fetch_remote_protocol_capabilities(base_url: str, *, timeout: int = 5) -> tuple[set[str], str | None]:
    url = f"{str(base_url).rstrip('/')}/protocol/status"
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            capabilities = {str(item).strip() for item in (body.get("capabilities") or []) if str(item).strip()}
            return capabilities, None
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("error") or body.get("detail") or str(exc)
        except Exception:
            detail = str(exc)
        return set(), f"http_{exc.code}: {detail}"
    except URLError as exc:
        return set(), str(exc)
    except Exception as exc:
        return set(), str(exc)
