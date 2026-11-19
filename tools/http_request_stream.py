"""Stream HTTP results to Run-scoped storage without replaying effects."""
from __future__ import annotations

import codecs
import tempfile
from typing import Any

from tools.web_fetch_stream import _bounded_preview, _retain_text, _run_spool_task


async def execute_streamed_http(
    args: dict[str, Any], *, max_response_bytes: int | None = None, retained_output=None,
):
    from tools.builtins import BuiltinExecutionResult, _positive_seconds

    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"
    timeout, error = _positive_seconds(args.get("timeout", 30), label="HTTP timeout")
    if error:
        return error
    if max_response_bytes is not None and (
        type(max_response_bytes) is not int or max_response_bytes < 1
    ):
        return "Error: configured HTTP max_response_bytes must be a positive integer"

    method = str(args.get("method", "GET")).upper()
    headers = args.get("headers") or {}
    body = args.get("body")
    status = None
    submitted = False
    make_capture = (
        retained_output[0].temporary_capture_file if retained_output is not None
        else lambda: tempfile.TemporaryFile(mode="w+b")
    )
    try:
        import httpx

        # Open the capture before the request: a storage failure here has no
        # network effect. Once submitted, never automatically replay a POST.
        with make_capture() as captured:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True,
                headers={"User-Agent": "HASHI/2.2"},
            ) as client:
                req_kwargs: dict[str, Any] = {"headers": headers}
                if body:
                    req_kwargs["content"] = body.encode() if isinstance(body, str) else body
                submitted = True
                async with client.stream(method, url, **req_kwargs) as response:
                    status = response.status_code
                    content_type = response.headers.get("content-type", "")
                    encoding = response.encoding or "utf-8"
                    try:
                        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
                    except LookupError:
                        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    prefix = f"Status: {status}\nContent-Type: {content_type}\n\n"
                    captured.write(prefix.encode("utf-8"))
                    characters = len(prefix)
                    received = 0
                    complete = True
                    observed_at_least = 0
                    chunk_size = min(65536, max_response_bytes + 1) if max_response_bytes else 65536
                    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                        observed_at_least = received + len(chunk)
                        if max_response_bytes is not None:
                            room = max_response_bytes - received
                            if len(chunk) > room:
                                chunk = chunk[:room]
                                complete = False
                        received += len(chunk)
                        text = decoder.decode(chunk, final=False)
                        if text:
                            captured.write(text.encode("utf-8"))
                            characters += len(text)
                        if not complete:
                            break
                    final = decoder.decode(b"", final=True)
                    if final:
                        captured.write(final.encode("utf-8"))
                        characters += len(final)
            if not complete:
                warning = (
                    f"\n[Response incomplete: selected {max_response_bytes}-byte "
                    "limit; the request may already have taken effect. No automatic retry.]"
                )
                captured.write(warning.encode("utf-8"))
                characters += len(warning)
            details = {
                "http_status": status,
                "response_complete": complete,
                "complete_content": complete,
                "response_limit_bytes": max_response_bytes,
                "response_bytes": received,
                "response_observed_at_least_bytes": observed_at_least,
                "timeout_seconds": timeout,
            }
            if characters <= 20000 or retained_output is None:
                captured.seek(0)
                return BuiltinExecutionResult(captured.read().decode("utf-8"), details)

            store, binding, source_tool = retained_output
            try:
                reference = await _run_spool_task(
                    _retain_text, store, binding, source_tool, captured, "",
                )
            except (OSError, ValueError, TimeoutError):
                reference = None
            preview = _bounded_preview("", captured)
            if reference:
                output = (
                    preview + f"\n[truncated by HASHI; use tool_output_read reference={reference} "
                    "offset=0; retained while this Run is active]"
                )
            else:
                output = preview + (
                    "\n[truncated by HASHI; complete-result retention is unavailable for this Run]"
                )
            return BuiltinExecutionResult(output, {
                **details,
                "complete_content": False,
                "output_continuation": reference,
                "output_retention": store.retention if reference else "unavailable",
            })
    except Exception as exc:
        effect = "unknown" if submitted else "not_dispatched"
        message = (
            "Error: HTTP response unavailable; the request may already have taken effect. "
            "Do not retry without checking"
            if submitted else "Error: HTTP request was not dispatched"
        )
        return BuiltinExecutionResult(
            f"{message} ({type(exc).__name__}).",
            {"http_status": status, "response_complete": False, "request_effect": effect},
        )
# HASHI-Fingerprint: hfp_2f819b5d806d422dafd8763232c30809
