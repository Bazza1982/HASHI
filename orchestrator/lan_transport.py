"""Connect explicitly configured LAN services through checked DNS addresses."""

from __future__ import annotations

import re
import socket
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

import aiohttp

_LAN_NETWORKS = tuple(ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10", "fc00::/7",
))
_METADATA_ADDRESSES = {
    ip_address("169.254.169.254"), ip_address("100.100.100.200"),
    ip_address("fd00:ec2::254"),
}


class LanDestinationError(ValueError):
    """No checked destination is available for a LAN-labelled service."""


def is_private_lan_address(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address not in _METADATA_ADDRESSES and any(
        address.version == network.version and address in network
        for network in _LAN_NETWORKS
    )


def lan_host_is_admissible(host: str | None) -> bool:
    """Validate configuration spelling; resolve hostnames only when connecting."""
    if not isinstance(host, str) or not host:
        return False
    try:
        ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError:
            return False
        return len(ascii_host) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in ascii_host.split(".")
        )
    return is_private_lan_address(host)


class PrivateLanResolver(aiohttp.abc.AbstractResolver):
    """Return only allowed numeric results to the connector that will use them."""

    def __init__(self, delegate=None, *, allow_localhost=False):
        self.delegate = delegate or aiohttp.resolver.DefaultResolver()
        self.allow_localhost = allow_localhost

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        records = await self.delegate.resolve(host, port, family)
        if self.allow_localhost and host.casefold().rstrip(".") == "localhost":
            permitted = [row for row in records if str(row.get("host") or "") in {
                "127.0.0.1", "::1",
            }]
        else:
            permitted = [
                row for row in records
                if is_private_lan_address(str(row.get("host") or ""))
            ]
        if not permitted:
            raise LanDestinationError("LAN hostname has no permitted private address")
        return permitted

    async def close(self):
        await self.delegate.close()


@asynccontextmanager
async def configured_media_session(session_factory, target, *, timeout):
    """Pin checked DNS results to actual LAN connections and never use a proxy."""
    options = {"timeout": timeout, "trust_env": False}
    connector = None
    resolver = None
    if target.get("location") == "lan":
        parsed = urlsplit(str(target.get("base_url") or ""))
        if (
            parsed.scheme not in ("http", "https") or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not lan_host_is_admissible(parsed.hostname)
        ):
            raise LanDestinationError("invalid LAN service destination")
        try:
            ip_address(parsed.hostname)
        except ValueError:
            # aiohttp connects to these results without a second DNS lookup,
            # while retaining the configured hostname for Host and TLS SNI.
            resolver = PrivateLanResolver()
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
            options["connector"] = connector
    try:
        async with session_factory(**options) as session:
            yield session
    finally:
        if connector is not None:
            await connector.close()
        if resolver is not None:
            await resolver.close()


@asynccontextmanager
async def local_or_lan_streaming_session():
    """A local/LAN-only client for selected streaming nodes and WebSockets."""
    resolver = PrivateLanResolver(allow_localhost=True)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    trace = aiohttp.TraceConfig()

    async def reject_redirect(_session, _context, _params):
        raise LanDestinationError("local or LAN streaming redirect rejected")

    trace.on_request_redirect.append(reject_redirect)
    try:
        timeout = aiohttp.ClientTimeout(total=25, connect=10, sock_read=15)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                        trace_configs=[trace], trust_env=False) as session:
            yield session
    finally:
        await connector.close()
        await resolver.close()
# HASHI-Fingerprint: hfp_a5a7d4a2bced4307aa72f51b3a133fb6
