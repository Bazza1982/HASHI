"""Fixed HTTP resource worker; no shell, ambient proxies or arbitrary client.

The helper executes only a plan made by Safety, inside a supervised native Job.
It exits after one transaction; Job-zero observation proves its DNS/TCP owners
are gone even when a response or cancellation was lost.
"""
from __future__ import annotations

import asyncio
import base64
from http.cookies import CookieError, SimpleCookie
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
from urllib.parse import quote, quote_plus, urldefrag, urljoin, urlsplit, urlunsplit

import aiohttp

from orchestrator.permissions.models import normalize_scope
from orchestrator.permissions.network_stream import fold_percent

NETWORK_TOOLS = frozenset({"http_request", "web_fetch"})
_DANGEROUS_HEADERS = frozenset({
    "host", "connection", "content-length", "transfer-encoding", "proxy-authorization",
    "proxy-connection", "cookie", "set-cookie", "authorization", "x-api-key", "x-auth-token",
})


class RequestNotSent(ValueError):
    """A fixed preflight rejected the request before HTTP headers/body were sent."""


def _loopback_host(host):
    if str(host or "").casefold() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _port(parsed):
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def origin(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or len(url) > 16384):
            raise ValueError
        return normalize_scope("network", f"{parsed.scheme}://{parsed.netloc}", ["fetch"], cwd=Path.cwd()).resource
    except (ValueError, TypeError) as error:
        raise RequestNotSent("an absolute HTTP(S) target without credentials is required") from error


def build_options(raw, secrets):
    if not isinstance(raw, dict) or set(raw) - {"private_origins", "credentials", "forbidden_origins"}:
        raise ValueError("invalid installed network policy")
    private = raw.get("private_origins", [])
    forbidden = raw.get("forbidden_origins", [])
    if (not isinstance(private, list) or not isinstance(forbidden, list)
            or any(not isinstance(item, str) for item in private + forbidden)):
        raise ValueError("network policy origins must be explicit")
    credentials = raw.get("credentials", {})
    if not isinstance(credentials, dict):
        raise ValueError("invalid installed credential capabilities")
    prepared = {}
    for name, spec in credentials.items():
        if (not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) is None
                or not isinstance(spec, dict) or set(spec) - {"origin", "methods", "secret_ref", "header", "prefix", "response_fields"}
                or not {"origin", "methods", "secret_ref", "header", "prefix"}.issubset(spec)):
            raise ValueError("invalid installed credential capability")
        target = origin(spec["origin"])
        if not target.startswith("https:"):
            raise ValueError("credential capabilities require verified HTTPS")
        methods = spec["methods"]
        if (not isinstance(methods, list) or not methods or set(methods) - {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
                or not isinstance(spec["header"], str) or spec["header"].casefold() not in {"authorization", "x-api-key", "x-auth-token"}
                or not isinstance(spec["prefix"], str) or any(c in spec["prefix"] for c in "\r\n")):
            raise ValueError("invalid credential target operation")
        value = secrets.get(spec["secret_ref"])
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n") or len(value) > 16384:
            raise ValueError("the installed business credential is unavailable")
        response_fields = spec.get("response_fields")
        if (response_fields is not None and (not isinstance(response_fields, list) or len(response_fields) > 32
                or any(not isinstance(field, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", field) is None for field in response_fields))):
            raise ValueError("credential response fields must be explicit JSON paths")
        prepared[name] = {"response_fields": response_fields, "origin": target, "methods": list(methods), "header": spec["header"],
                          "value": spec["prefix"] + value, "redact": value}
    return {"private_origins": [origin(item) for item in private],
            "forbidden_origins": [origin(item) for item in forbidden], "credentials": prepared}


def prepare(tool_name, arguments, options):
    allowed = {"url"} if tool_name == "web_fetch" else {
        "url", "method", "headers", "body", "timeout", "credential", "follow_redirects",
        "max_redirects", "session_cookies", "response_encoding", "body_file", "response_file",
    }
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise RequestNotSent("unsupported managed HTTP arguments")
    target = origin(arguments.get("url"))
    follow_redirects = arguments.get("follow_redirects", False)
    session_cookies = arguments.get("session_cookies", False)
    max_redirects = arguments.get("max_redirects", 20)
    response_encoding = arguments.get("response_encoding", "text")
    if response_encoding not in ("text", "base64"):
        raise RequestNotSent("response encoding must be text or base64")
    if (type(follow_redirects) is not bool or type(session_cookies) is not bool
            or type(max_redirects) is not int or max_redirects < 1):
        raise RequestNotSent("redirect choice, session choice or budget is invalid")
    method = arguments.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise RequestNotSent("unsupported HTTP operation")
    method = method.upper()
    headers, body, timeout = arguments.get("headers"), arguments.get("body"), arguments.get("timeout", 30)
    file_body = arguments.get("body_file")
    file_response = arguments.get("response_file")
    if "response_file" in arguments and (not isinstance(file_response, str) or not file_response.strip()):
        raise RequestNotSent("HTTP response file must be an explicit file")
    if "body_file" in arguments and (
        not isinstance(file_body, str) or not file_body.strip() or body is not None
    ):
        raise RequestNotSent("HTTP file body must be one explicit file without an inline body")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict) or type(timeout) not in (int, float):
        raise RequestNotSent("invalid HTTP headers or idle timeout")
    try:
        timeout = float(timeout)
    except OverflowError as error:
        raise RequestNotSent("invalid HTTP headers or idle timeout") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise RequestNotSent("invalid HTTP headers or idle timeout")
    for key, value in headers.items():
        if (not isinstance(key, str) or not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}", key) is None
                or key.casefold() in _DANGEROUS_HEADERS or any(c in value for c in "\r\n") or len(value) > 8192):
            raise RequestNotSent("unsafe HTTP header; use an installed credential capability")
    if len(json.dumps(body, allow_nan=False).encode()) > 1024 * 1024:
        raise RequestNotSent("HTTP body exceeds the supervised bound")
    redact, response_fields, credential_methods = [], None, None
    credential = arguments.get("credential")
    if credential is not None:
        installed = options.get("credentials", {}).get(credential)
        if installed is None or installed["origin"] != target or method not in installed["methods"]:
            raise RequestNotSent("credential is not installed for this exact target operation")
        headers = {**headers, installed["header"]: installed["value"]}
        redact = [installed["value"], installed["redact"]]
        response_fields = installed["response_fields"]
        credential_methods = list(installed["methods"])
    if file_response is not None and response_fields is not None:
        raise RequestNotSent("streaming selected JSON fields is not available")
    forbidden = options.get("forbidden_origins", [])
    parsed_target = urlsplit(target)
    is_loopback_target = _loopback_host(parsed_target.hostname)
    forbidden_local_port = is_loopback_target and any(
        _loopback_host(urlsplit(item).hostname) and _port(urlsplit(item)) == _port(parsed_target)
        for item in forbidden
    )
    if target in forbidden or forbidden_local_port:
        raise RequestNotSent("the target is part of the trusted control plane")
    allow_private = target in options.get("private_origins", [])
    return {"url": arguments["url"], "origin": target, "method": method, "headers": headers,
            "body": body, "timeout": float(timeout), "redact": redact, "response_fields": response_fields,
            "follow_redirects": follow_redirects, "max_redirects": max_redirects,
            "session_cookies": session_cookies, "credential_methods": credential_methods,
            "response_encoding": response_encoding,
            **({"body_source": "bound"} if file_body is not None else {}),
            **({"response_sink": "bound"} if file_response is not None else {}),
            "allow_private": allow_private, "allow_loopback": allow_private and is_loopback_target}


def address_permitted(value, allow_private, allow_loopback=False):
    address = ipaddress.ip_address(value)
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return bool(allow_loopback)
    if address.is_unspecified or address.is_link_local or address.is_multicast or address.is_reserved:
        return False
    return address.is_global or bool(allow_private)


def _secret_patterns(secrets):
    patterns = set()
    for secret in secrets:
        secret_bytes = secret.encode("utf-8")
        patterns.update((secret, json.dumps(secret, ensure_ascii=False)[1:-1],
                         json.dumps(secret, ensure_ascii=True)[1:-1],
                         quote(secret, safe=""), quote_plus(secret, safe=""),
                         base64.b64encode(secret_bytes).decode("ascii"),
                         base64.urlsafe_b64encode(secret_bytes).decode("ascii").rstrip("=")))
    patterns.discard("")
    return patterns


def _trailing_secret_prefix(text, secret):
    """Length of the longest end fragment that starts a known secret."""
    failure = [0] * len(secret)
    matched = 0
    for index in range(1, len(secret)):
        while matched and secret[index] != secret[matched]:
            matched = failure[matched - 1]
        if secret[index] == secret[matched]:
            matched += 1
        failure[index] = matched
    matched = 0
    for char in text[-max(0, len(secret) - 1):] if len(secret) > 1 else "":
        while matched and char != secret[matched]:
            matched = failure[matched - 1]
        if char == secret[matched]:
            matched += 1
        if matched == len(secret):
            matched = failure[matched - 1]
    return matched


def _redact_value(value, patterns, *, leading=False, trailing=False):
    """Redact known secrets and, for partial responses, fragments at each edge."""
    patterns = sorted((item for item in patterns if item), key=len, reverse=True)
    if not patterns:
        return value
    binary = isinstance(value, bytes)
    default = b"[credential redacted]" if binary else "[credential redacted]"

    percent = b"%" if binary else "%"

    def contains(candidate, pattern):
        return (fold_percent(pattern) in fold_percent(candidate)
                if percent in pattern else pattern in candidate)

    def replace(candidate, pattern, marker):
        if percent not in pattern:
            return candidate.replace(pattern, marker)
        folded, needle = fold_percent(candidate), fold_percent(pattern)
        pieces, cursor = [], 0
        while True:
            position = folded.find(needle, cursor)
            if position < 0:
                pieces.append(candidate[cursor:])
                break
            pieces.extend((candidate[cursor:position], marker))
            cursor = position + len(pattern)
        return (b"" if binary else "").join(pieces)

    def edge(candidate, pattern, *, reverse=False):
        if percent in pattern:
            candidate, pattern = candidate.lower(), pattern.lower()
        if reverse:
            candidate, pattern = candidate[::-1], pattern[::-1]
        return _trailing_secret_prefix(candidate, pattern)

    def redact(marker):
        result = value
        for pattern in patterns:
            result = replace(result, pattern, marker)
        if leading:
            fragment = max(edge(result, item, reverse=True) for item in patterns)
            if fragment:
                result = marker + result[fragment:]
        if trailing:
            fragment = max(edge(result, item) for item in patterns)
            if fragment:
                result = result[:-fragment] + marker
        return result

    result = redact(default)
    if all(not contains(result, item) for item in patterns):
        return result

    # The descriptive marker can itself contain, or join adjacent text into,
    # a credential. A single sentinel absent from every pattern cannot create
    # a match across its boundary.
    used = set().union(*(set(item) for item in patterns))
    for item in patterns:
        if percent in item:
            used.update(item.lower())
            used.update(item.upper())
    choices = (range(256) if binary else (*range(33, 127), *range(0xE000, 0xF900)))
    for number in choices:
        marker = bytes((number,)) if binary else chr(number)
        member = number if binary else marker
        if member not in used:
            result = redact(marker)
            if all(not contains(result, item) for item in patterns):
                return result
    # If no safe sentinel exists, withhold this value rather than leak a key.
    return b"" if binary else ""


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, host, port, answers):
        self.host, self.port, self.answers = host, port, answers

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host.casefold() != self.host.casefold() or port != self.port:
            raise RequestNotSent("HTTP transport tried another target")
        return list(self.answers)

    async def close(self):
        pass


async def run(plan, *, body_reader=None, response_sink=None):
    sink = plan.get("response_sink")
    if (sink not in (None, "bound") or (sink == "bound") != (response_sink is not None)
            or (response_sink is not None and (
                not callable(getattr(response_sink, "write", None))
                or not callable(getattr(response_sink, "commit", None))
            ))):
        raise RequestNotSent("the approved HTTP response file is unavailable")
    if response_sink is not None and plan.get("response_fields") is not None:
        raise RequestNotSent("streaming selected JSON fields is not available")
    source = plan.get("body_source")
    if source not in (None, "bound") or (source == "bound") != (body_reader is not None):
        raise RequestNotSent("the approved HTTP body file is unavailable")
    body_size = None
    if body_reader is not None:
        try:
            body_size = body_reader.identity()["size"]
        except Exception as error:
            raise RequestNotSent("the approved HTTP body file is unavailable") from error
        if type(body_size) is not int or not 0 <= body_size < 2**63:
            raise RequestNotSent("the approved HTTP body file size is invalid")

    async def body_pages():
        offset = 0
        while offset < body_size:
            size = min(1024 * 1024, body_size - offset)
            chunk = await asyncio.to_thread(body_reader.read_at, offset=offset, max_bytes=size)
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > size:
                raise OSError("the approved HTTP body file changed during upload")
            offset += len(chunk)
            yield chunk

    parts = urlsplit(plan["url"])
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    try:
        values = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise RequestNotSent("the target could not be resolved") from error
    answers = []
    for family, _, proto, _, address in values:
        if (not address_permitted(address[0], plan["allow_private"], plan.get("allow_loopback", False))
                or (plan.get("allow_loopback") and not _loopback_host(address[0]))):
            raise RequestNotSent("DNS resolved outside the installed network boundary")
        item = {"hostname": host, "host": address[0], "port": port, "family": family,
                "proto": proto, "flags": socket.AI_NUMERICHOST}
        if item not in answers:
            answers.append(item)
    if not answers:
        raise RequestNotSent("the target has no usable address")
    resolver = _PinnedResolver(host, port, answers)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, force_close=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=plan["timeout"], sock_read=plan["timeout"])
    try:
        ipaddress.ip_address(host)
        allow_ip_cookie = bool(plan.get("allow_private"))
    except ValueError:
        allow_ip_cookie = False
    jar = (aiohttp.CookieJar(unsafe=allow_ip_cookie)
           if plan.get("session_cookies") else aiohttp.DummyCookieJar())
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                                     cookie_jar=jar,
                                     auto_decompress=plan.get("response_encoding") != "base64") as session:
        body = plan["body"]
        request = {"headers": plan["headers"], "allow_redirects": False}
        if (plan.get("response_encoding") == "base64"
                and not any(key.casefold() == "accept-encoding" for key in request["headers"])):
            # Ranges and their validators refer to the wire representation.
            request["headers"] = {**request["headers"], "Accept-Encoding": "identity"}
        if body_reader is not None:
            request["headers"] = {**request["headers"], "Content-Length": str(body_size)}
            request["data"] = body_pages()
        elif isinstance(body, (dict, list, bool, int, float)):
            request["json"] = body
        elif body is not None:
            if not isinstance(body, str):
                raise RequestNotSent("HTTP body must be text or JSON")
            request["data"] = body.encode("utf-8")
        current_url = plan["url"]
        current_method = plan["method"]
        visited = {(current_method, current_url)}
        redirects = []
        observed_cookies = set()
        while True:
            async with session.request(current_method, current_url, **request) as response:
                if plan.get("session_cookies"):
                    observed_cookies.update(item.value for item in jar if item.value)
                # The server can set a credential cookie even when the
                # caller did not ask to retain it for redirects.
                observed_cookies.update(
                    item.value for item in getattr(response, "cookies", {}).values() if item.value
                )
                # A response can set the same cookie name more than once;
                # the client jar keeps only the last, but every value can
                # appear in the response body and needs redaction.
                getall = getattr(response.headers, "getall", None)
                raw_cookies = (getall("Set-Cookie", []) if callable(getall)
                               else [response.headers.get("Set-Cookie", "")])
                for raw in raw_cookies:
                    parsed_cookie = SimpleCookie()
                    try:
                        parsed_cookie.load(raw)
                    except CookieError:
                        continue
                    observed_cookies.update(item.value for item in parsed_cookie.values() if item.value)
                redirect_stop_reason = None
                location = response.headers.get("Location")
                if (plan.get("follow_redirects") and response.status in {301, 302, 303, 307, 308}
                        and location):
                    next_method = current_method
                    if current_method not in {"GET", "HEAD"}:
                        if response.status == 303:
                            next_method = "GET"
                            if (plan.get("credential_methods") is not None
                                    and "GET" not in plan["credential_methods"]):
                                redirect_stop_reason = "credential_method_not_granted"
                        else:
                            redirect_stop_reason = "method_not_replayed"
                    elif "data" in request or "json" in request:
                        redirect_stop_reason = "method_not_replayed"
                    if not redirect_stop_reason:
                        try:
                            destination = urldefrag(urljoin(current_url, location))[0]
                            destination_origin = origin(destination)
                        except (TypeError, ValueError, RequestNotSent):
                            destination = None
                            destination_origin = None
                        if destination_origin is None:
                            redirect_stop_reason = "invalid_destination"
                        elif destination_origin != plan["origin"]:
                            redirect_stop_reason = "different_origin"
                        else:
                            # Equivalent host spellings use the pinned authority.
                            next_parts = urlsplit(destination)
                            destination = urlunsplit((parts.scheme, parts.netloc, next_parts.path,
                                                      next_parts.query, ""))
                        if redirect_stop_reason:
                            pass
                        elif (next_method, destination) in visited:
                            redirect_stop_reason = "loop"
                        elif len(redirects) >= plan["max_redirects"]:
                            redirect_stop_reason = "limit"
                        else:
                            # Keep the resolver pinned to the already admitted host.
                            # A 303 continues as a read; it never resends the body.
                            redirects.append({"status": response.status, "method": current_method})
                            current_url, current_method = destination, next_method
                            visited.add((current_method, current_url))
                            if current_method == "GET" and "json" in request:
                                request.pop("json")
                            if current_method == "GET" and "data" in request:
                                request.pop("data")
                            if current_method == "GET":
                                request["headers"] = {
                                    key: value for key, value in request["headers"].items()
                                    if key.casefold() not in {"content-type", "content-encoding", "content-length"}
                                }
                            continue
                if response_sink is not None:
                    from orchestrator.permissions.network_stream import StreamingRedactor

                    patterns = _secret_patterns([*plan["redact"], *observed_cookies])
                    headers = {key: _redact_value(value, patterns) for key, value in response.headers.items()
                               if key.casefold() not in _DANGEROUS_HEADERS
                               and key.casefold() in {
                                   "content-type", "content-encoding", "content-range", "accept-ranges",
                                   "location", "etag", "last-modified", "retry-after",
                               }}
                    encoding = response.headers.get("Content-Encoding", "").strip().casefold()
                    if plan.get("response_encoding") == "base64" and patterns and encoding not in {"", "identity"}:
                        return json.dumps({
                            "status": response.status, "headers": headers, "body_saved": False,
                            "body_redacted": True, "truncated": True,
                            "content_unavailable_reason": "encoded_sensitive_response",
                        }, ensure_ascii=False)
                    scrubber = StreamingRedactor(
                        (item.encode("utf-8") for item in patterns), partial=response.status == 206,
                    )
                    captured = saved = 0

                    def save_pages(data):
                        for start in range(0, len(data), 1024 * 1024):
                            response_sink.write(data[start:start + 1024 * 1024])
                        return len(data)

                    async for chunk in response.content.iter_chunked(16384):
                        captured += len(chunk)
                        saved += save_pages(scrubber.feed(chunk))
                    saved += save_pages(scrubber.feed(b"", final=True))
                    response_sink.commit()
                    return json.dumps({
                        "status": response.status, "headers": headers, "body_saved": True,
                        "captured_bytes": captured, "saved_bytes": saved,
                        "body_redacted": scrubber.redacted, "truncated": False,
                        "redirect_followed": bool(redirects),
                        **({"redirects": redirects} if redirects else {}),
                        **({"redirect_stop_reason": redirect_stop_reason} if redirect_stop_reason else {}),
                    }, ensure_ascii=False)
                data = bytearray()
                capture_limit = 512 * 1024
                async for chunk in response.content.iter_chunked(16384):
                    # One look-ahead byte distinguishes an exactly full
                    # response from a genuinely incomplete preview.
                    data.extend(chunk[:max(0, capture_limit + 1 - len(data))])
                    if len(data) > capture_limit:
                        break
                truncated = len(data) > capture_limit
                if truncated:
                    del data[capture_limit:]
                text = data.decode("utf-8", errors="replace")
                headers = {key: value for key, value in response.headers.items()
                           if key.casefold() not in _DANGEROUS_HEADERS
                           and key.casefold() in {
                               "content-type", "content-encoding", "content-range", "accept-ranges",
                               "location", "etag", "last-modified", "retry-after",
                           }}
                if plan["response_fields"] is not None:
                    try:
                        parsed = json.loads(text)
                    except (ValueError, TypeError):
                        parsed = {}
                    selected = {}
                    for field in plan["response_fields"]:
                        value = parsed
                        for key in field.split("."):
                            value = value.get(key) if isinstance(value, dict) else None
                        selected[field] = value
                    text = json.dumps(selected, ensure_ascii=False)
                patterns = _secret_patterns([*plan["redact"], *observed_cookies])
                if plan.get("response_encoding") == "base64":
                    encoding = next((value.strip().casefold() for key, value in response.headers.items()
                                     if key.casefold() == "content-encoding"), "")
                    if patterns and encoding not in {"", "identity"}:
                        # Encoded content can conceal a credential from byte
                        # redaction. Ask for identity and never return concealed
                        # bytes if a server ignores that request.
                        headers = {key: _redact_value(value, patterns) for key, value in headers.items()}
                        return json.dumps({
                            "status": response.status, "headers": headers, "body": "",
                            "body_encoding": "base64", "captured_bytes": len(data),
                            "body_redacted": True, "truncated": True,
                            "content_unavailable_reason": "encoded_sensitive_response",
                            "redirect_followed": bool(redirects),
                            **({"redirects": redirects} if redirects else {}),
                            **({"redirect_stop_reason": redirect_stop_reason} if redirect_stop_reason else {}),
                        }, ensure_ascii=False)
                    # Return exact response bytes unless a credential policy
                    # deliberately selects fields or a secret must be redacted.
                    selected = (text.encode("utf-8") if plan["response_fields"] is not None else bytes(data))
                    original_selected = selected
                    byte_patterns = sorted((item.encode("utf-8") for item in patterns), key=len, reverse=True)
                    selected = _redact_value(
                        selected, byte_patterns, leading=response.status == 206,
                        trailing=truncated or response.status == 206,
                    )
                    redacted = plan["response_fields"] is not None or selected != original_selected
                    headers = {key: _redact_value(value, patterns) for key, value in headers.items()}
                    while True:
                        result = json.dumps({
                            "status": response.status, "headers": headers,
                            "body": base64.b64encode(selected).decode("ascii"), "body_encoding": "base64",
                            "captured_bytes": len(data), "body_redacted": redacted,
                            "truncated": truncated, "redirect_followed": bool(redirects),
                            **({"redirects": redirects} if redirects else {}),
                            **({"redirect_stop_reason": redirect_stop_reason} if redirect_stop_reason else {}),
                        }, ensure_ascii=False)
                        if len(result.encode("utf-8")) < 900000:
                            return result
                        selected, truncated = selected[:len(selected) // 2], True
                        safe = _redact_value(selected, byte_patterns, trailing=True)
                        redacted = redacted or safe != selected
                        selected = safe
                original_text = text
                text = _redact_value(
                    text, patterns, leading=response.status == 206,
                    trailing=truncated or response.status == 206,
                )
                redacted = plan["response_fields"] is not None or text != original_text
                headers = {key: _redact_value(value, patterns) for key, value in headers.items()}
                while True:
                    result = json.dumps({"status": response.status, "headers": headers, "body": text,
                                          "body_redacted": redacted,
                                          "truncated": truncated, "redirect_followed": bool(redirects),
                                         **({"redirects": redirects} if redirects else {}),
                                         **({"redirect_stop_reason": redirect_stop_reason}
                                            if redirect_stop_reason else {})}, ensure_ascii=False)
                    if len(result.encode("utf-8")) < 900000:
                        return result
                    text, truncated = text[:len(text)//2], True
                    safe = _redact_value(text, patterns, trailing=True)
                    redacted = redacted or safe != text
                    text = safe
# HASHI-Fingerprint: hfp_600078b99c0c43d2b5fe21cfd5b8fa4c
