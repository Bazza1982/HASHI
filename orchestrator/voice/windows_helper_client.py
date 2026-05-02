from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


@dataclass(frozen=True)
class WindowsHelperClient:
    base_url: str = "http://127.0.0.1:47831"
    timeout: float = 5.0

    def action(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"action": action, "args": args or {}}).encode("utf-8")
        req = request.Request(
            f"{self.base_url.rstrip('/')}/action",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"windows helper action failed: HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"windows helper unavailable at {self.base_url}: {exc}") from exc

        outer = json.loads(raw)
        if not outer.get("ok"):
            raise RuntimeError(f"windows helper action failed: {outer}")
        output = outer.get("output")
        if isinstance(output, str):
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                parsed = {"text": output}
        elif isinstance(output, dict):
            parsed = output
        else:
            parsed = {"output": output}
        parsed["_helper_request_id"] = outer.get("request_id")
        parsed["_helper_elapsed_ms"] = outer.get("elapsed_ms")
        return parsed

    def health(self) -> dict[str, Any]:
        req = request.Request(f"{self.base_url.rstrip('/')}/health", method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except URLError as exc:
            raise RuntimeError(f"windows helper unavailable at {self.base_url}: {exc}") from exc
