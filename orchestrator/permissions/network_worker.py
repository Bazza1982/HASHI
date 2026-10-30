"""Fixed HTTP resource worker; no shell, redirects, proxies or arbitrary client.

The helper executes only a plan made by Safety, inside a supervised native Job.
It exits after one transaction; Job-zero observation proves its DNS/TCP owners
are gone even when a response or cancellation was lost.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

import aiohttp

from orchestrator.permissions.models import normalize_scope

NETWORK_TOOLS = frozenset({"http_request", "web_fetch"})
_DANGEROUS_HEADERS = frozenset({
    "host", "connection", "content-length", "transfer-encoding", "proxy-authorization",
    "proxy-connection", "cookie", "set-cookie", "authorization", "x-api-key", "x-auth-token",
})


class RequestNotSent(ValueError):
    """A fixed preflight rejected the request before HTTP headers/body were sent."""


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
        response_fields = spec.get("response_fields", [])
        if (not isinstance(response_fields, list) or len(response_fields) > 32
                or any(not isinstance(field, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", field) is None for field in response_fields)):
            raise ValueError("credential response fields must be explicit scalar JSON paths")
        prepared[name] = {"response_fields": response_fields, "origin": target, "methods": list(methods), "header": spec["header"],
                          "value": spec["prefix"] + value, "redact": value}
    return {"private_origins": [origin(item) for item in private],
            "forbidden_origins": [origin(item) for item in forbidden], "credentials": prepared}


def prepare(tool_name, arguments, options):
    allowed = {"url"} if tool_name == "web_fetch" else {"url", "method", "headers", "body", "timeout", "credential"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise RequestNotSent("unsupported managed HTTP arguments")
    target = origin(arguments.get("url"))
    method = arguments.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        raise RequestNotSent("unsupported HTTP operation")
    method = method.upper()
    headers, body, timeout = arguments.get("headers") or {}, arguments.get("body"), arguments.get("timeout", 30)
    if (not isinstance(headers, dict) or len(headers) > 32
            or type(timeout) not in (int, float) or not 1 <= timeout <= 300):
        raise RequestNotSent("invalid HTTP headers or idle timeout")
    for key, value in headers.items():
        if (not isinstance(key, str) or not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}", key) is None
                or key.casefold() in _DANGEROUS_HEADERS or any(c in value for c in "\r\n") or len(value) > 8192):
            raise RequestNotSent("unsafe HTTP header; use an installed credential capability")
    if len(json.dumps(body, allow_nan=False).encode()) > 1024 * 1024:
        raise RequestNotSent("HTTP body exceeds the supervised bound")
    redact, response_fields = [], None
    credential = arguments.get("credential")
    if credential is not None:
        installed = options.get("credentials", {}).get(credential)
        if installed is None or installed["origin"] != target or method not in installed["methods"]:
            raise RequestNotSent("credential is not installed for this exact target operation")
        headers = {**headers, installed["header"]: installed["value"]}
        redact = [installed["value"], installed["redact"]]
        response_fields = installed["response_fields"]
    if target in options.get("forbidden_origins", []):
        raise RequestNotSent("the target is part of the trusted control plane")
    return {"url": arguments["url"], "origin": target, "method": method, "headers": headers,
            "body": body, "timeout": float(timeout), "redact": redact, "response_fields": response_fields,
            "allow_private": target in options.get("private_origins", [])}


def address_permitted(value, allow_private):
    address = ipaddress.ip_address(value)
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast or address.is_reserved:
        return False
    return address.is_global or bool(allow_private)


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, host, port, answers):
        self.host, self.port, self.answers = host, port, answers

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host.casefold() != self.host.casefold() or port != self.port:
            raise RequestNotSent("HTTP transport tried another target")
        return list(self.answers)

    async def close(self):
        pass


async def run(plan):
    parts = urlsplit(plan["url"])
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    try:
        values = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise RequestNotSent("the target could not be resolved") from error
    answers = []
    for family, _, proto, _, address in values:
        if not address_permitted(address[0], plan["allow_private"]):
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
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                                     cookie_jar=aiohttp.DummyCookieJar()) as session:
        body = plan["body"]
        request = {"headers": plan["headers"], "allow_redirects": False}
        if isinstance(body, (dict, list, bool, int, float)):
            request["json"] = body
        elif body is not None:
            if not isinstance(body, str):
                raise RequestNotSent("HTTP body must be text or JSON")
            request["data"] = body.encode("utf-8")
        async with session.request(plan["method"], plan["url"], **request) as response:
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk[:max(0, 512 * 1024 - len(data))])
                if len(data) >= 512 * 1024:
                    break
            text = data.decode("utf-8", errors="replace")
            headers = {key: value for key, value in response.headers.items()
                       if key.casefold() not in _DANGEROUS_HEADERS
                       and key.casefold() in {"content-type", "location", "etag", "last-modified", "retry-after"}}
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
                    if value is None or type(value) in (str, bool, int, float):
                        selected[field] = value
                text = json.dumps(selected, ensure_ascii=False)
            for secret in plan["redact"]:
                for encoded in (secret, json.dumps(secret, ensure_ascii=False)[1:-1]):
                    text = text.replace(encoded, "[credential redacted]")
                    headers = {key: value.replace(encoded, "[credential redacted]") for key, value in headers.items()}
            truncated = len(data) >= 512 * 1024
            while True:
                result = json.dumps({"status": response.status, "headers": headers, "body": text,
                                     "truncated": truncated, "redirect_followed": False}, ensure_ascii=False)
                if len(result.encode("utf-8")) < 900000:
                    return result
                text, truncated = text[:len(text)//2], True
